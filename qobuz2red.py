import json
import os
import shutil
import subprocess
import sys

from torf import Torrent

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


def load_config():
    """Load configuration from config.json."""
    if not os.path.exists(CONFIG_PATH):
        print(f"Error: Config file not found at {CONFIG_PATH}")
        sys.exit(1)
    
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
        ["qobuz-dl", "dl","--no-db", url, "-d", download_dir],
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
    
    if not flac_files:
        print("Warning: No FLAC files found in album folder.")
        return
    
    print(f"Found {len(flac_files)} FLAC file(s) to recompress.")
    
    for i, flac_file in enumerate(flac_files, 1):
        print(f"  [{i}/{len(flac_files)}] {flac_file}")
        file_path = os.path.join(album_folder, flac_file)
        subprocess.run(
            [flac_path, "-f8", file_path],
            check=True
        )


def move_album(album_folder, destination_dir):
    """Move the album folder to the destination directory."""
    if not os.path.exists(destination_dir):
        os.makedirs(destination_dir)
    
    album_name = os.path.basename(album_folder)
    destination_path = os.path.join(destination_dir, album_name)
    
    shutil.move(album_folder, destination_path)
    return destination_path


def get_folder_size(folder_path):
    """Calculate total size of all files in a folder in bytes."""
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(folder_path):
        for filename in filenames:
            file_path = os.path.join(dirpath, filename)
            total_size += os.path.getsize(file_path)
    return total_size


def get_piece_size(total_size):
    """Get recommended piece size based on RED's guidelines."""
    MiB = 1024 * 1024
    GiB = 1024 * MiB
    KiB = 1024
    
    if total_size <= 50 * MiB:
        return 32 * KiB
    elif total_size <= 150 * MiB:
        return 64 * KiB
    elif total_size <= 350 * MiB:
        return 128 * KiB
    elif total_size <= 512 * MiB:
        return 256 * KiB
    elif total_size <= 1 * GiB:
        return 512 * KiB
    elif total_size <= 2 * GiB:
        return 1024 * KiB
    else:
        return 2048 * KiB


def create_torrent(album_folder, announce_url, output_dir):
    """Create a RED-compliant torrent file."""
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    folder_size = get_folder_size(album_folder)
    piece_size = get_piece_size(folder_size)
    
    album_name = os.path.basename(album_folder)
    torrent_path = os.path.join(output_dir, f"{album_name}.torrent")
    
    t = Torrent(path=album_folder)
    t.trackers = [announce_url]
    t.source = "RED"
    t.private = True
    t.piece_size = piece_size
    t.generate()
    t.write(torrent_path)
    
    return torrent_path


def main():
    try:
        config = load_config()
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in config file: {e}")
        sys.exit(1)
    
    download_dir = config["qobuz_download_dir"]
    destination_dir = config["destination_dir"]
    flac_path = config["flac_path"]
    announce_url = config["announce_url"]
    torrent_output_dir = config["torrent_output_dir"]
    
    url = input("Enter Qobuz album URL: ").strip()
    
    if not url:
        print("Error: No URL provided.")
        sys.exit(1)
    
    try:
        print("Downloading album...")
        album_folder = download_album(url, download_dir)
        
        if not album_folder:
            print("Error: Could not detect downloaded album folder.")
            sys.exit(1)
        
        print(f"Downloaded to: {album_folder}")
        
        print("Recompressing FLAC files to level 8...")
        recompress_flac_files(album_folder, flac_path)
        print("Recompression complete.")
        
        print(f"Moving album to {destination_dir}...")
        final_path = move_album(album_folder, destination_dir)
        print(f"Album moved to: {final_path}")
        
        print("Creating torrent file...")
        torrent_path = create_torrent(final_path, announce_url, torrent_output_dir)
        print(f"Torrent created: {torrent_path}")
        
        print("\nDone!")
        
    except subprocess.CalledProcessError as e:
        print(f"Error: Command failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
