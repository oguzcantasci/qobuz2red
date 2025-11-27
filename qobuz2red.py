import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


def load_config():
    """Load configuration from config.json."""
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def get_existing_folders(directory):
    """Get set of existing folder names in directory."""
    if not os.path.exists(directory):
        return set()
    return set(
        name for name in os.listdir(directory)
        if os.path.isdir(os.path.join(directory, name))
    )


def download_album(url, download_dir):
    """Download album using qobuz-dl."""
    folders_before = get_existing_folders(download_dir)
    
    subprocess.run(
        ["qobuz-dl", "dl", url, "-d", download_dir],
        check=True
    )
    
    folders_after = get_existing_folders(download_dir)
    new_folders = folders_after - folders_before
    
    if not new_folders:
        return None
    
    return os.path.join(download_dir, new_folders.pop())


def recompress_flac_files(album_folder, flac_path):
    """Recompress all FLAC files in the album folder to level 8."""
    flac_files = [
        f for f in os.listdir(album_folder)
        if f.lower().endswith(".flac")
    ]
    
    for flac_file in flac_files:
        file_path = os.path.join(album_folder, flac_file)
        subprocess.run(
            [flac_path, "-f8", file_path],
            check=True
        )


def main():
    config = load_config()
    
    download_dir = config["qobuz_download_dir"]
    destination_dir = config["destination_dir"]
    flac_path = config["flac_path"]
    
    url = input("Enter Qobuz album URL: ").strip()
    
    print(f"Downloading album...")
    album_folder = download_album(url, download_dir)
    
    if album_folder:
        print(f"Downloaded to: {album_folder}")
        
        print("Recompressing FLAC files to level 8...")
        recompress_flac_files(album_folder, flac_path)
        print("Recompression complete.")


if __name__ == "__main__":
    main()

