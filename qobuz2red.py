import json
import os
import shutil
import subprocess
import sys

import requests
from bs4 import BeautifulSoup
from mutagen.flac import FLAC
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.text import Text
from torf import Torrent

# Rich console for styled output
console = Console()

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
        console.print("[yellow]Warning:[/yellow] No FLAC files found in album folder.")
        return
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console
    ) as progress:
        task = progress.add_task(
            f"[cyan]Recompressing {len(flac_files)} FLAC files...",
            total=len(flac_files)
        )
        
        for flac_file in flac_files:
            file_path = os.path.join(album_folder, flac_file)
            subprocess.run(
                [flac_path, "-f8", file_path],
                check=True,
                capture_output=True  # Suppress flac output for cleaner progress bar
            )
            progress.advance(task)


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
        console.print(f"[yellow]Warning:[/yellow] Could not fetch album cover: {e}")
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
        
        # Find all track containers (div.track__items)
        track_elements = soup.find_all('div', class_='track__items')
        
        for track in track_elements:
            # Get track number
            number_elem = track.find('div', class_='track__item--number')
            if number_elem:
                number_span = number_elem.find('span')
                track_num = number_span.get_text(strip=True) if number_span else ""
            else:
                track_num = ""
            
            # Get track name
            name_elem = track.find('div', class_='track__item--name')
            if name_elem:
                # Get the first span (track title), not the "Explicit" span
                title_span = name_elem.find('span', class_=lambda x: x != 'explicit' if x else True)
                title = title_span.get_text(strip=True) if title_span else ""
                
                # Check if explicit
                explicit_span = name_elem.find('span', class_='explicit')
                is_explicit = explicit_span is not None
            else:
                title = ""
                is_explicit = False
            
            # Get duration
            duration_elem = track.find('span', class_='track__item--duration')
            duration = duration_elem.get_text(strip=True) if duration_elem else "00:00:00"
            
            # Build track line
            if title:
                if is_explicit:
                    track_line = f"{track_num}. {title} (Explicit) — {duration}"
                else:
                    track_line = f"{track_num}. {title} — {duration}"
                tracks.append(track_line)
        
        if tracks:
            return "Tracklist:\n\n" + "\n".join(tracks)
        
        return None
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Could not fetch tracklist: {e}")
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
        result = Prompt.ask(f"[cyan]{field_name}[/cyan]", default=str(default_value))
        return result
    else:
        while True:
            result = Prompt.ask(f"[cyan]{field_name}[/cyan]", default="")
            if result or not required:
                return result
            console.print(f"[yellow]  {field_name} is required.[/yellow]")


def prompt_multiline(field_name, default=None):
    """Prompt user for multi-line input. Empty line to finish."""
    if default:
        console.print(f"\n[cyan]{field_name}[/cyan] - [green]Default from Qobuz:[/green]")
        console.print(Panel(default, border_style="dim"))
        use_default = Prompt.ask(
            f"Use this {field_name.lower()}?",
            choices=["y", "n", "edit"],
            default="y"
        )
        if use_default == 'n':
            return ""
        elif use_default == 'edit':
            console.print(f"[dim]Enter new {field_name} (press Enter twice to finish):[/dim]")
        else:
            return default
    else:
        console.print(f"[cyan]{field_name}[/cyan] [dim](press Enter twice to finish, or just Enter to skip):[/dim]")
    
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
    table = Table(title="Release Types", border_style="dim")
    table.add_column("ID", style="cyan", width=4)
    table.add_column("Type", style="white")
    
    for key, value in RELEASE_TYPES.items():
        table.add_row(str(key), value)
    
    console.print()
    console.print(table)
    
    while True:
        try:
            choice = int(Prompt.ask("Select release type", default="1"))
            if choice in RELEASE_TYPES:
                return choice
            console.print("[yellow]Invalid choice. Please select a valid release type.[/yellow]")
        except ValueError:
            console.print("[yellow]Please enter a number.[/yellow]")


def prompt_upload_fields(metadata, qobuz_url=None):
    """Prompt user to confirm/edit all upload fields."""
    console.print()
    console.print(Panel("[bold]UPLOAD DETAILS[/bold] - Confirm or edit each field", border_style="cyan"))
    console.print()
    
    # Derive defaults from metadata
    bitrate = get_bitrate_string(metadata["bits_per_sample"])
    release_desc = get_release_description(
        metadata["bits_per_sample"], 
        metadata["sample_rate"],
        qobuz_url
    )
    
    # Try to get album cover and tracklist from Qobuz
    console.print("[cyan]🔍[/cyan] Fetching album info from Qobuz...")
    album_cover = get_qobuz_cover(qobuz_url)
    tracklist = get_qobuz_tracklist(qobuz_url)
    
    if album_cover:
        console.print(f"[green]✓[/green] Found album cover")
    else:
        console.print("[dim]Could not fetch album cover automatically.[/dim]")
    
    if tracklist:
        console.print("[green]✓[/green] Found tracklist from Qobuz page")
    else:
        console.print("[dim]Could not fetch tracklist automatically.[/dim]")
    
    fields = {}
    
    console.print()
    
    # Category (type)
    fields["type"] = 0  # Music
    console.print("[magenta]●[/magenta] [white]Category:[/white] [green]Music[/green]")
    
    # Artist
    fields["artists[]"] = prompt_field("Artist", metadata["artist"])
    
    # Artist importance (1 = Main)
    fields["importance[]"] = 1
    console.print("[magenta]●[/magenta] [white]Artist importance:[/white] [green]Main[/green]")
    
    # Album title
    fields["title"] = prompt_field("Album Title", metadata["album"])
    
    # Original year
    fields["year"] = prompt_field("Original Year", metadata["year"])
    
    # Release type
    fields["releasetype"] = prompt_release_type()
    
    # Unknown release
    fields["unknown"] = "0"
    console.print("[magenta]●[/magenta] [white]Unknown release:[/white] [green]No[/green]")
    
    # Edition/Remaster info
    console.print("\n[bold yellow]Edition Info[/bold yellow]")
    fields["remaster_year"] = prompt_field("Edition Year", metadata["year"], required=False)
    fields["remaster_title"] = prompt_field("Edition Title", "", required=False)
    fields["remaster_record_label"] = prompt_field("Record Label", metadata["label"], required=False)
    fields["remaster_catalogue_number"] = prompt_field("Catalogue Number", "", required=False)
    
    # Scene release
    fields["scene"] = "0"
    console.print("[magenta]●[/magenta] [white]Scene release:[/white] [green]No[/green]")
    
    # Format info
    console.print("\n[bold yellow]Format Info[/bold yellow]")
    fields["format"] = "FLAC"
    console.print("[magenta]●[/magenta] [white]Format:[/white] [green]FLAC[/green]")
    
    # Bitrate
    fields["bitrate"] = bitrate
    console.print(f"[magenta]●[/magenta] [white]Bitrate:[/white] [green]{bitrate}[/green]")
    
    # Media
    fields["media"] = "WEB"
    console.print("[magenta]●[/magenta] [white]Media:[/white] [green]WEB[/green]")
    
    # Additional info
    console.print("\n[bold yellow]Additional Info[/bold yellow]")
    
    # Tags (genre)
    fields["tags"] = prompt_field("Tags (comma-separated)", metadata["genre"], required=False)
    
    # Image URL
    fields["image"] = prompt_field("Image URL", album_cover or "", required=False)
    
    # Descriptions
    console.print("\n[bold yellow]Descriptions[/bold yellow]")
    
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
    # Display header
    console.print()
    console.print(Panel.fit(
        "[bold cyan]Qobuz to RED Uploader[/bold cyan]\n[dim]Download, recompress, and upload to RED[/dim]",
        border_style="cyan"
    ))
    console.print()
    
    try:
        config = load_config()
    except json.JSONDecodeError as e:
        console.print(f"[red]Error:[/red] Invalid JSON in config file: {e}")
        sys.exit(1)
    
    download_dir = config["qobuz_download_dir"]
    destination_dir = config["destination_dir"]
    flac_path = config["flac_path"]
    announce_url = config["announce_url"]
    torrent_output_dir = config["torrent_output_dir"]
    api_key = config["api_key"]
    debug = config.get("debug", False)
    watch_folder = config.get("watch_folder")
    
    # Check for existing albums in destination (sorted by date, newest first, max 10)
    existing_albums = get_existing_folders(destination_dir)
    
    if existing_albums:
        # Sort by modification time (newest first)
        albums_with_time = []
        for album in existing_albums:
            album_path = os.path.join(destination_dir, album)
            mtime = os.path.getmtime(album_path)
            albums_with_time.append((album, mtime))
        
        albums_with_time.sort(key=lambda x: x[1], reverse=True)
        recent_albums = [album for album, _ in albums_with_time[:10]]
        
        # Display albums in a table
        table = Table(title="Recent Albums (10 most recent)", border_style="blue")
        table.add_column("#", style="cyan", width=4)
        table.add_column("Album", style="white")
        
        for i, album in enumerate(recent_albums, 1):
            table.add_row(str(i), album)
        
        console.print()
        console.print(table)
        console.print()
        console.print("[dim]Enter album number to use existing, or press Enter to download new[/dim]")
        
        use_existing = Prompt.ask(
            "[cyan]Selection[/cyan]",
            default="",
            show_default=False
        )
        
        if use_existing:
            try:
                album_index = int(use_existing) - 1
                album_name = recent_albums[album_index]
                final_path = os.path.join(destination_dir, album_name)
                console.print(f"\n[green]✓[/green] Using existing album: [bold]{final_path}[/bold]")
            except (ValueError, IndexError):
                console.print("[yellow]Invalid selection, proceeding with download...[/yellow]")
                use_existing = None
    else:
        use_existing = None
    
    if not use_existing:
        url = Prompt.ask("[cyan]Enter Qobuz album URL[/cyan]")
        
        if not url:
            console.print("[red]Error:[/red] No URL provided.")
            sys.exit(1)
    else:
        # Ask for URL for release description when using existing album
        url = Prompt.ask("[cyan]Enter Qobuz album URL[/cyan] [dim](for metadata, optional)[/dim]", default="") or None
    
    try:
        if not use_existing:
            console.print("\n[cyan]⬇[/cyan]  Downloading album...")
            album_folder = download_album(url, download_dir)
            
            if not album_folder:
                console.print("[red]Error:[/red] Could not detect downloaded album folder.")
                sys.exit(1)
            
            console.print(f"[green]✓[/green] Downloaded to: [dim]{album_folder}[/dim]")
            
            console.print("\n[cyan]🔄[/cyan] Recompressing FLAC files...")
            recompress_flac_files(album_folder, flac_path)
            console.print("[green]✓[/green] Recompression complete.")
            
            console.print(f"\n[cyan]📁[/cyan] Moving album to destination...")
            final_path = move_album(album_folder, destination_dir)
            console.print(f"[green]✓[/green] Album moved to: [dim]{final_path}[/dim]")
        
        # Check if torrent already exists
        album_name = os.path.basename(final_path)
        expected_torrent_path = os.path.join(torrent_output_dir, f"{album_name}.torrent")
        
        if os.path.exists(expected_torrent_path):
            console.print(f"\n[yellow]Torrent already exists:[/yellow] {expected_torrent_path}")
            use_existing_torrent = Confirm.ask("Use existing torrent?", default=True)
            if use_existing_torrent:
                torrent_path = expected_torrent_path
                console.print(f"[green]✓[/green] Using existing torrent")
            else:
                console.print("[cyan]📦[/cyan] Creating new torrent file...")
                torrent_path = create_torrent(final_path, announce_url, torrent_output_dir)
                console.print(f"[green]✓[/green] Torrent created: [dim]{torrent_path}[/dim]")
        else:
            console.print("\n[cyan]📦[/cyan] Creating torrent file...")
            torrent_path = create_torrent(final_path, announce_url, torrent_output_dir)
            console.print(f"[green]✓[/green] Torrent created: [dim]{torrent_path}[/dim]")
        
        # Read metadata for upload
        console.print("\n[cyan]🔍[/cyan] Reading FLAC metadata...")
        metadata = read_flac_metadata(final_path)
        
        if not metadata:
            console.print("[red]Error:[/red] Could not read FLAC metadata.")
            sys.exit(1)
        
        # Prompt for upload fields
        upload_fields = prompt_upload_fields(metadata, url)
        
        # Ask if user wants to do a dry run first
        console.print()
        do_dry_run = Confirm.ask("[cyan]Do dry run first?[/cyan]", default=True)
        
        if do_dry_run:
            console.print()
            console.print(Panel("[bold]PERFORMING DRY RUN...[/bold]", border_style="yellow"))
            
            dry_run_result = upload_torrent(torrent_path, upload_fields, api_key, dry_run=True, debug=debug)
            
            console.print("\n[bold]Dry run result:[/bold]")
            console.print_json(json.dumps(dry_run_result, indent=2))
            
            if dry_run_result.get("status") != "success":
                console.print(f"\n[red]✗ Dry run failed:[/red] {dry_run_result.get('error', 'Unknown error')}")
                sys.exit(1)
        
        # Ask to proceed with actual upload
        console.print()
        proceed = Confirm.ask("[bold cyan]Proceed with actual upload?[/bold cyan]", default=False)
        
        if proceed:
            console.print("\n[cyan]⬆[/cyan]  Uploading to RED...")
            upload_result = upload_torrent(torrent_path, upload_fields, api_key, dry_run=False, debug=debug)
            
            if upload_result.get("status") == "success":
                response = upload_result.get("response", {})
                console.print()
                console.print(Panel.fit(
                    f"[bold green]✓ Upload Successful![/bold green]\n\n"
                    f"[white]Torrent ID:[/white] [cyan]{response.get('torrentid')}[/cyan]\n"
                    f"[white]Group ID:[/white] [cyan]{response.get('groupid')}[/cyan]",
                    border_style="green"
                ))
                
                # Move torrent to watch folder if configured
                if watch_folder:
                    if not os.path.exists(watch_folder):
                        os.makedirs(watch_folder)
                    torrent_filename = os.path.basename(torrent_path)
                    watch_path = os.path.join(watch_folder, torrent_filename)
                    shutil.move(torrent_path, watch_path)
                    console.print(f"[green]✓[/green] Torrent moved to: [dim]{watch_path}[/dim]")
            else:
                console.print(f"\n[red]✗ Upload failed:[/red] {upload_result.get('error', 'Unknown error')}")
                sys.exit(1)
        else:
            console.print("\n[yellow]Upload cancelled.[/yellow]")
        
        console.print("\n[bold green]Done![/bold green]")
        
    except subprocess.CalledProcessError as e:
        console.print(f"\n[red]✗ Error:[/red] Command failed: {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[red]✗ Error:[/red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
