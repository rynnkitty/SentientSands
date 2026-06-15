import os
import sys
import subprocess
import urllib.request
import urllib.error
import zipfile
import shutil
import ssl

# SSL bypass for corporate/Windows environments where cert verification fails.
# This is download-only; production TLS is handled by the embedded Python runtime.
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

def _download(url, dest):
    """urlretrieve wrapper that tolerates SSL cert issues."""
    try:
        urllib.request.urlretrieve(url, dest)
    except Exception:
        # Fallback: open with unverified context
        with urllib.request.urlopen(url, context=_SSL_CTX) as r, open(dest, 'wb') as f:
            f.write(r.read())

# Configuration
PYTHON_VERSION = "3.10.11"
EMBEDDED_ZIP_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET_DIR = os.path.join(SCRIPT_DIR, "python")
REQUIREMENTS_PATH = os.path.join(SCRIPT_DIR, "requirements.txt")

# Embedding model (used by Faction RAG, world_lore RAG, DurableMemory vector recall)
MODEL_REPO_ID = "minishlab/potion-multilingual-128M"
MODEL_LOCAL_DIR = os.path.join(SCRIPT_DIR, "models", "potion-multilingual-128M")


def setup_python():
    print(f"--- Setting up Embedded Python {PYTHON_VERSION} ---")

    if os.path.exists(TARGET_DIR):
        print(f"Target directory {TARGET_DIR} already exists. Cleaning it...")
        shutil.rmtree(TARGET_DIR)

    os.makedirs(TARGET_DIR)

    zip_path = os.path.join(SCRIPT_DIR, "python_embedded.zip")
    print(f"Downloading embedded Python from {EMBEDDED_ZIP_URL}...")
    _download(EMBEDDED_ZIP_URL, zip_path)

    print("Extracting...")
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(TARGET_DIR)
    os.remove(zip_path)

    # Fix .pth file to enable site-packages
    pth_file = os.path.join(TARGET_DIR, "python310._pth")
    if os.path.exists(pth_file):
        print(f"Updating {pth_file} to enable site-packages...")
        with open(pth_file, 'r') as f:
            lines = f.readlines()
        with open(pth_file, 'w') as f:
            for line in lines:
                if line.strip() == "#import site":
                    f.write("import site\n")
                else:
                    f.write(line)
            if not any("import site" in l for l in lines):
                f.write("import site\n")

    print("Downloading get-pip.py...")
    pip_loader = os.path.join(TARGET_DIR, "get-pip.py")
    _download(GET_PIP_URL, pip_loader)

    print("Installing pip...")
    python_exe = os.path.join(TARGET_DIR, "python.exe")
    subprocess.run([python_exe, pip_loader], check=True)
    os.remove(pip_loader)

    print("Installing requirements (flask, model2vec, sqlite-vec, rapidfuzz, ...)...")
    subprocess.run(
        [python_exe, "-m", "pip", "install", "-r", REQUIREMENTS_PATH],
        check=True
    )

    print("--- Embedded Python Setup Complete ---")
    print(f"Location: {TARGET_DIR}")


def download_model():
    """Download potion-multilingual-128M from HuggingFace Hub if not present."""
    print("\n--- Embedding Model Setup ---")

    # Check if model already downloaded (presence of model.safetensors is the key file)
    key_file = os.path.join(MODEL_LOCAL_DIR, "model.safetensors")
    if os.path.exists(key_file):
        size_mb = os.path.getsize(key_file) // (1024 * 1024)
        print(f"Model already exists ({size_mb} MB). Skipping download.")
        return True

    os.makedirs(MODEL_LOCAL_DIR, exist_ok=True)
    print(f"Downloading {MODEL_REPO_ID} (~537 MB) from HuggingFace Hub...")
    print("This may take several minutes depending on your connection speed.")

    python_exe = os.path.join(TARGET_DIR, "python.exe")

    # Use huggingface_hub (installed as model2vec dependency) to download
    download_script = r"""
import sys, os
from huggingface_hub import snapshot_download

repo_id = sys.argv[1]
local_dir = sys.argv[2]

# Remove offline flag if set — we need network access during setup
os.environ.pop("HF_HUB_OFFLINE", None)

print(f"Downloading {repo_id} to {local_dir} ...")
snapshot_download(
    repo_id=repo_id,
    local_dir=local_dir,
    ignore_patterns=["*.gguf", "*.bin", "flax_model*", "tf_model*", "rust_model*"],
)
print("Download complete.")
"""
    script_path = os.path.join(SCRIPT_DIR, "_dl_model_tmp.py")
    try:
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(download_script)

        result = subprocess.run(
            [python_exe, script_path, MODEL_REPO_ID, MODEL_LOCAL_DIR],
            check=False
        )
        if result.returncode != 0:
            print("\nWARNING: Model download failed.")
            print("The server will still work but Faction RAG semantic matching and")
            print("DurableMemory vector recall will be unavailable.")
            print(f"To download manually later, run:")
            print(f"  server\\python\\python.exe -c \"from huggingface_hub import snapshot_download; snapshot_download('{MODEL_REPO_ID}', local_dir=r'{MODEL_LOCAL_DIR}')\"")
            return False
        else:
            print(f"Model saved to: {MODEL_LOCAL_DIR}")
            return True
    finally:
        if os.path.exists(script_path):
            os.remove(script_path)


def main():
    # Step 1: Set up embedded Python + install packages
    setup_python()

    # Step 2: Download embedding model
    download_model()

    print("\n========================================")
    print("  SentientSands Setup Complete!")
    print("========================================")
    print("Next step: Launch Kenshi with the mod enabled.")
    print("The server will start automatically when you load a save.")


if __name__ == "__main__":
    main()
