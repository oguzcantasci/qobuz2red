import json
import os
import shutil
import subprocess
import sys

import requests
from bs4 import BeautifulSoup
from mutagen.flac import FLAC
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


def read_flac_metadata(album_folder):
    """Read metadata from the first FLAC file in the album folder."""
    flac_files = [
        f for f in os.listdir(album_folder)
        if f.lower().endswith(".flac")
    ]
    
    if not flac_files:
        return None
    
    flac_path = os.path.join(album_folder, flac_files[0])
    audio = FLAC(flac_path)
    
    # Get tags (FLAC tags are lists, so we take the first value)
    def get_tag(tag_name):
        values = audio.get(tag_name, [])
        return values[0] if values else ""
    
    # Get audio info
    bits_per_sample = audio.info.bits_per_sample
    sample_rate = audio.info.sample_rate
    
    metadata = {
        "artist": get_tag("artist"),
        "album": get_tag("album"),
        "year": get_tag("date")[:4] if get_tag("date") else get_tag("year"),
        "label": get_tag("label") or get_tag("organization"),
        "genre": get_tag("genre"),
        "bits_per_sample": bits_per_sample,
        "sample_rate": sample_rate,
    }
    
    return metadata


def get_bitrate_string(bits_per_sample):
    """Get RED bitrate string based on bit depth."""
    if bits_per_sample == 24:
        return "24bit Lossless"
    return "Lossless"


def get_release_description(bits_per_sample, sample_rate, qobuz_url=None):
    """Generate release description like '24/96 Qobuz Rip [url]'."""
    sample_rate_khz = sample_rate / 1000
    # Format sample rate nicely (44.1, 48, 96, etc.)
    if sample_rate_khz == int(sample_rate_khz):
        sample_rate_str = str(int(sample_rate_khz))
    else:
        sample_rate_str = str(sample_rate_khz)
    
    desc = f"{bits_per_sample}/{sample_rate_str} Qobuz Rip"
    if qobuz_url:
        desc += f" [url]{qobuz_url}[/url]"
    return desc


def get_qobuz_cover(url):
    """Extract album cover image URL from Qobuz album page."""
    if not url:
        return None
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Try to find the album cover image
        img = soup.find(class_='album-cover__image')
        if img and img.get('src'):
            return img['src']
        
        # Fallback: look for og:image meta tag
        og_image = soup.find('meta', property='og:image')
        if og_image and og_image.get('content'):
            return og_image['content']
        
        return None
    except Exception as e:
        print(f"Warning: Could not fetch album cover: {e}")
        return None


def get_qobuz_tracklist(url):
    """Extract tracklist from Qobuz album page."""
    if not url:
        return None
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        tracks = []
        
        # Find all track rows - they typically have track number, title, and duration
        # Look for track items in the page
        track_elements = soup.find_all('div', class_='track__item')
        
        if not track_elements:
            # Alternative: look for track rows
            track_elements = soup.find_all(class_='track')
        
        for i, track in enumerate(track_elements, 1):
            # Try to get track title
            title_elem = track.find(class_='track__item--name') or track.find(class_='track-name')
            if not title_elem:
                # Try finding any element with track title
                title_elem = track.find('span', class_=lambda x: x and 'name' in x.lower() if x else False)
            
            # Try to get duration
            duration_elem = track.find(class_='track__item--duration') or track.find(class_='track-duration')
            if not duration_elem:
                # Look for time format pattern
                duration_elem = track.find(string=lambda x: x and ':' in x and len(x.strip()) in [5, 7, 8] if x else False)
            
            title = title_elem.get_text(strip=True) if title_elem else f"Track {i}"
            
            # Get duration text
            if duration_elem:
                if hasattr(duration_elem, 'get_text'):
                    duration = duration_elem.get_text(strip=True)
                else:
                    duration = str(duration_elem).strip()
            else:
                duration = "00:00"
            
            # Clean up duration - ensure format is consistent
            if duration and not duration.startswith('00:'):
                # If duration is like "3:37", convert to "00:03:37"
                if len(duration) <= 5:
                    duration = f"00:{duration.zfill(5)}"
            
            tracks.append(f"{i}. {title} — {duration}")
        
        if tracks:
            return "Tracklist:\n\n" + "\n".join(tracks)
        
        return None
    except Exception as e:
        print(f"Warning: Could not fetch tracklist: {e}")
        return None


# Release type mappings for RED
RELEASE_TYPES = {
    1: "Album",
    3: "Soundtrack",
    5: "EP",
    6: "Anthology",
    7: "Compilation",
    9: "Single",
    11: "Live album",
    13: "Remix",
    14: "Bootleg",
    15: "Interview",
    16: "Mixtape",
    17: "Demo",
    18: "Concert Recording",
    19: "DJ Mix",
    21: "Unknown",
}


def prompt_field(field_name, default_value, required=True):
    """Prompt user for a field value with a default."""
    if default_value:
        user_input = input(f"{field_name} [{default_value}]: ").strip()
        return user_input if user_input else default_value
    else:
        while True:
            user_input = input(f"{field_name}: ").strip()
            if user_input or not required:
                return user_input
            print(f"  {field_name} is required.")


def prompt_multiline(field_name, default=None):
    """Prompt user for multi-line input. Empty line to finish."""
    if default:
        print(f"\n{field_name} - Default from Qobuz:")
        print("-" * 40)
        print(default)
        print("-" * 40)
        use_default = input(f"Use this {field_name.lower()}? (Y/n/edit): ").strip().lower()
        if use_default == 'n':
            return ""
        elif use_default == 'edit':
            print(f"Enter new {field_name} (press Enter twice to finish):")
        else:
            return default
    else:
        print(f"{field_name} (press Enter twice to finish, or just Enter to skip):")
    
    lines = []
    while True:
        line = input()
        if line == "":
            if not lines:  # First empty line with no content = skip
                return ""
            break  # Empty line after content = done
        lines.append(line)
    return "\n".join(lines)


def prompt_release_type():
    """Prompt user to select a release type."""
    print("\nRelease Types:")
    for key, value in RELEASE_TYPES.items():
        print(f"  {key}: {value}")
    
    while True:
        try:
            choice = int(input("Select release type [1]: ").strip() or "1")
            if choice in RELEASE_TYPES:
                return choice
            print("  Invalid choice. Please select a valid release type.")
        except ValueError:
            print("  Please enter a number.")


def prompt_upload_fields(metadata, qobuz_url=None):
    """Prompt user to confirm/edit all upload fields."""
    print("\n" + "="*50)
    print("UPLOAD DETAILS - Confirm or edit each field")
    print("="*50 + "\n")
    
    # Derive defaults from metadata
    bitrate = get_bitrate_string(metadata["bits_per_sample"])
    release_desc = get_release_description(
        metadata["bits_per_sample"], 
        metadata["sample_rate"],
        qobuz_url
    )
    
    # Try to get album cover and tracklist from Qobuz
    print("Fetching album info from Qobuz...")
    album_cover = get_qobuz_cover(qobuz_url)
    tracklist = get_qobuz_tracklist(qobuz_url)
    
    if album_cover:
        print(f"Found album cover: {album_cover[:50]}...")
    else:
        print("Could not fetch album cover automatically.")
    
    if tracklist:
        print("Found tracklist from Qobuz page.")
    else:
        print("Could not fetch tracklist automatically.")
    
    fields = {}
    
    # Category (type)
    fields["type"] = 0  # Music
    print(f"Category: Music (0)")
    
    # Artist
    fields["artists[]"] = prompt_field("Artist", metadata["artist"])
    
    # Artist importance (1 = Main)
    fields["importance[]"] = 1
    print(f"Artist importance: Main (1)")
    
    # Album title
    fields["title"] = prompt_field("Album Title", metadata["album"])
    
    # Original year
    fields["year"] = prompt_field("Original Year", metadata["year"])
    
    # Release type
    fields["releasetype"] = prompt_release_type()
    
    # Unknown release
    fields["unknown"] = "0"
    print(f"Unknown release: No")
    
    # Edition/Remaster info
    fields["remaster_year"] = prompt_field("Edition Year", metadata["year"], required=False)
    fields["remaster_title"] = prompt_field("Edition Title", "", required=False)
    fields["remaster_record_label"] = prompt_field("Record Label", metadata["label"], required=False)
    fields["remaster_catalogue_number"] = prompt_field("Catalogue Number", "", required=False)
    
    # Scene release
    fields["scene"] = "0"
    print(f"Scene release: No")
    
    # Format
    fields["format"] = "FLAC"
    print(f"Format: FLAC")
    
    # Bitrate
    fields["bitrate"] = bitrate
    print(f"Bitrate: {bitrate}")
    
    # Media
    fields["media"] = "WEB"
    print(f"Media: WEB")
    
    # Tags (genre)
    fields["tags"] = prompt_field("Tags (comma-separated)", metadata["genre"], required=False)
    
    # Image URL
    fields["image"] = prompt_field("Image URL", album_cover or "", required=False)
    
    # Album description (multi-line, with tracklist default if available)
    fields["album_desc"] = prompt_multiline("Album Description", tracklist)
    
    # Release description
    fields["release_desc"] = prompt_field("Release Description", release_desc, required=False)
    
    # Group ID (for adding to existing group)
    add_to_group = input("\nAdd to existing group? (y/N): ").strip().lower()
    if add_to_group == 'y':
        fields["groupid"] = prompt_field("Group ID", "", required=True)
    
    return fields


RED_API_URL = "https://redacted.sh/ajax.php"


def upload_torrent(torrent_path, fields, api_key, dry_run=False, debug=False):
    """Upload torrent to RED via API."""
    headers = {
        "Authorization": api_key
    }
    
    # Prepare form data
    data = {
        "dryrun": "1" if dry_run else "0",
        "type": fields["type"],
        "artists[]": fields["artists[]"],
        "importance[]": fields["importance[]"],
        "title": fields["title"],
        "year": fields["year"],
        "releasetype": fields["releasetype"],
        "unknown": fields.get("unknown", "0"),
        "scene": fields.get("scene", "0"),
        "format": fields["format"],
        "bitrate": fields["bitrate"],
        "media": fields["media"],
    }
    
    # Add optional fields if provided
    if fields.get("remaster_year"):
        data["remaster_year"] = fields["remaster_year"]
    if fields.get("remaster_title"):
        data["remaster_title"] = fields["remaster_title"]
    if fields.get("remaster_record_label"):
        data["remaster_record_label"] = fields["remaster_record_label"]
    if fields.get("remaster_catalogue_number"):
        data["remaster_catalogue_number"] = fields["remaster_catalogue_number"]
    if fields.get("tags"):
        data["tags"] = fields["tags"]
    if fields.get("image"):
        data["image"] = fields["image"]
    if fields.get("album_desc"):
        data["album_desc"] = fields["album_desc"]
    if fields.get("release_desc"):
        data["release_desc"] = fields["release_desc"]
    if fields.get("groupid"):
        data["groupid"] = fields["groupid"]
    
    # Debug output
    if debug:
        print("\n" + "="*50)
        print("DEBUG: API REQUEST DATA")
        print("="*50)
        print(f"URL: {RED_API_URL}?action=upload")
        print(f"Torrent file: {torrent_path}")
        print("\nForm data:")
        for key, value in data.items():
            print(f"  {key}: {value}")
        print("="*50 + "\n")
    
    # Open torrent file
    with open(torrent_path, "rb") as f:
        files = {
            "file_input": (os.path.basename(torrent_path), f, "application/x-bittorrent")
        }
        
        response = requests.post(
            f"{RED_API_URL}?action=upload",
            headers=headers,
            data=data,
            files=files
        )
    
    result = response.json()
    return result


def create_torrent(album_folder, announce_url, output_dir):
    """Create a RED-compliant torrent file."""
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    folder_size = get_folder_size(album_folder)
    piece_size = get_piece_size(folder_size)
    
    album_name = os.path.basename(album_folder)
    torrent_path = os.path.join(output_dir, f"{album_name}.torrent")
    
    # Remove existing torrent if present (for overwrite)
    if os.path.exists(torrent_path):
        os.remove(torrent_path)
    
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
    api_key = config["api_key"]
    debug = config.get("debug", False)
    watch_folder = config.get("watch_folder")
    
    # Check for existing albums in destination
    existing_albums = get_existing_folders(destination_dir)
    
    if existing_albums:
        print("\nExisting albums in destination:")
        for i, album in enumerate(sorted(existing_albums), 1):
            print(f"  {i}. {album}")
        
        use_existing = input("\nUse existing album? Enter number (or press Enter to download new): ").strip()
        
        if use_existing:
            try:
                album_index = int(use_existing) - 1
                album_name = sorted(existing_albums)[album_index]
                final_path = os.path.join(destination_dir, album_name)
                print(f"\nUsing existing album: {final_path}")
            except (ValueError, IndexError):
                print("Invalid selection, proceeding with download...")
                use_existing = None
    else:
        use_existing = None
    
    if not use_existing:
        url = input("Enter Qobuz album URL: ").strip()
        
        if not url:
            print("Error: No URL provided.")
            sys.exit(1)
    else:
        # Ask for URL for release description when using existing album
        url = input("Enter Qobuz album URL (for release description, optional): ").strip() or None
    
    try:
        if not use_existing:
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
        
        # Check if torrent already exists
        album_name = os.path.basename(final_path)
        expected_torrent_path = os.path.join(torrent_output_dir, f"{album_name}.torrent")
        
        if os.path.exists(expected_torrent_path):
            use_existing_torrent = input(f"\nTorrent already exists: {expected_torrent_path}\nUse existing? (Y/n): ").strip().lower()
            if use_existing_torrent != 'n':
                torrent_path = expected_torrent_path
                print(f"Using existing torrent: {torrent_path}")
            else:
                print("Creating new torrent file...")
                torrent_path = create_torrent(final_path, announce_url, torrent_output_dir)
                print(f"Torrent created: {torrent_path}")
        else:
            print("Creating torrent file...")
            torrent_path = create_torrent(final_path, announce_url, torrent_output_dir)
            print(f"Torrent created: {torrent_path}")
        
        # Read metadata for upload
        print("\nReading FLAC metadata...")
        metadata = read_flac_metadata(final_path)
        
        if not metadata:
            print("Error: Could not read FLAC metadata.")
            sys.exit(1)
        
        # Prompt for upload fields
        upload_fields = prompt_upload_fields(metadata, url)
        
        # Ask if user wants to do a dry run first
        do_dry_run = input("\nDo dry run first? (Y/n): ").strip().lower()
        
        if do_dry_run != 'n':
            print("\n" + "="*50)
            print("PERFORMING DRY RUN...")
            print("="*50)
            
            dry_run_result = upload_torrent(torrent_path, upload_fields, api_key, dry_run=True, debug=debug)
            
            print(f"\nDry run result:")
            print(json.dumps(dry_run_result, indent=2))
            
            if dry_run_result.get("status") != "success":
                print(f"\nDry run failed: {dry_run_result.get('error', 'Unknown error')}")
                sys.exit(1)
        
        # Ask to proceed with actual upload
        proceed = input("\nProceed with actual upload? (y/N): ").strip().lower()
        
        if proceed == 'y':
            print("\nUploading to RED...")
            upload_result = upload_torrent(torrent_path, upload_fields, api_key, dry_run=False, debug=debug)
            
            if upload_result.get("status") == "success":
                response = upload_result.get("response", {})
                print(f"\nUpload successful!")
                print(f"  Torrent ID: {response.get('torrentid')}")
                print(f"  Group ID: {response.get('groupid')}")
                
                # Move torrent to watch folder if configured
                if watch_folder:
                    if not os.path.exists(watch_folder):
                        os.makedirs(watch_folder)
                    torrent_filename = os.path.basename(torrent_path)
                    watch_path = os.path.join(watch_folder, torrent_filename)
                    shutil.move(torrent_path, watch_path)
                    print(f"  Torrent moved to: {watch_path}")
            else:
                print(f"\nUpload failed: {upload_result.get('error', 'Unknown error')}")
                sys.exit(1)
        else:
            print("\nUpload cancelled.")
        
        print("\nDone!")
        
    except subprocess.CalledProcessError as e:
        print(f"Error: Command failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
