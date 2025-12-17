import json
import os
import shutil
import subprocess
import sys
from urllib.parse import urlparse

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


def read_batch_links(batch_file):
    """Read non-commented links from batch file."""
    if not batch_file or not os.path.exists(batch_file):
        return []
    
    links = []
    with open(batch_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # Skip empty lines and comments
            if line and not line.startswith("#"):
                links.append(line)
    return links


def mark_link_processed(batch_file, link):
    """Comment out a processed link in the batch file."""
    if not batch_file or not os.path.exists(batch_file):
        return
    
    with open(batch_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    with open(batch_file, "w", encoding="utf-8") as f:
        for line in lines:
            if line.strip() == link:
                f.write(f"# {line}" if not line.endswith("\n") else f"# {line.rstrip()}\n")
            else:
                f.write(line)


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


def flatten_nested_album_folder(album_folder):
    """
    Detect and flatten nested folder structures caused by backslashes in metadata.
    
    When qobuz-dl encounters a backslash in album/artist names (e.g., "AC\DC"),
    Windows interprets it as a path separator, creating nested folders like:
    download_dir/AC/DC - Album/ instead of download_dir/AC-DC - Album/
    
    This function detects such cases and flattens them.
    """
    while True:
        contents = os.listdir(album_folder)
        
        # Check if folder contains only a single subfolder (and no files)
        if len(contents) == 1:
            single_item = os.path.join(album_folder, contents[0])
            if os.path.isdir(single_item):
                # This looks like an unwanted nested structure
                # Flatten by combining folder names with dash
                parent_name = os.path.basename(album_folder)
                child_name = contents[0]
                new_name = f"{parent_name}-{child_name}"
                
                # Move child contents up to parent level with new combined name
                parent_dir = os.path.dirname(album_folder)
                new_path = os.path.join(parent_dir, new_name)
                
                # Safety check: ensure target doesn't already exist
                if os.path.exists(new_path):
                    # Add suffix to avoid collision
                    counter = 1
                    while os.path.exists(f"{new_path}_{counter}"):
                        counter += 1
                    new_path = f"{new_path}_{counter}"
                    new_name = os.path.basename(new_path)
                
                # Move the nested folder up with the combined name
                shutil.move(single_item, new_path)
                
                # Remove the now-empty parent folder
                os.rmdir(album_folder)
                
                console.print(f"[yellow]Note:[/yellow] Flattened nested folder: '{parent_name}/{child_name}' → '{new_name}'")
                
                # Continue checking in case there are multiple levels of nesting
                album_folder = new_path
                continue
        
        # No more nesting detected
        break
    
    return album_folder


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


def is_valid_qobuz_url(url):
    """Check if URL is a valid Qobuz URL."""
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.netloc.endswith('qobuz.com')


def handle_parse_qobuz_page(batch_file=None, batch_parse_file=None):
    """
    Handle the [P] parse page option.
    Prompts for URL or reads from batch_parse file, parses albums, shows them,
    and lets user choose action.
    
    Returns:
        list: Album links to process, or empty list if cancelled/saved to file.
    """
    # Check if batch_parse file has links
    parse_links = read_batch_links(batch_parse_file)
    page_urls = []
    
    if parse_links:
        console.print(f"\n[dim]Found {len(parse_links)} page URLs in batch parse file[/dim]")
        console.print("[dim]  [Enter] Enter single URL[/dim]")
        console.print("[dim]  [F] Parse all from file[/dim]")
        source_choice = Prompt.ask("[cyan]Source[/cyan]", default="", show_default=False)
        
        if source_choice.upper() == "F":
            page_urls = parse_links
        else:
            page_url = Prompt.ask("[cyan]Enter Qobuz artist/label page URL[/cyan]")
            if page_url:
                page_urls = [page_url]
    else:
        page_url = Prompt.ask("[cyan]Enter Qobuz artist/label page URL[/cyan]")
        if page_url:
            page_urls = [page_url]
    
    if not page_urls:
        return []
    
    # Parse all page URLs and collect album links
    all_album_links = []
    processed_page_urls = []
    
    for i, url in enumerate(page_urls, 1):
        if len(page_urls) > 1:
            console.print(f"\n[cyan]🔍[/cyan] Parsing page {i}/{len(page_urls)}: [dim]{url}[/dim]")
        else:
            console.print("[cyan]🔍[/cyan] Parsing page for album links...")
        
        album_links = parse_qobuz_page(url)
        
        if album_links:
            console.print(f"[green]✓[/green] Found {len(album_links)} albums")
            all_album_links.extend(album_links)
            processed_page_urls.append(url)
        else:
            console.print("[yellow]No album links found on this page[/yellow]")
    
    if not all_album_links:
        console.print("[yellow]No album links found[/yellow]")
        return []
    
    # Deduplicate album links
    seen = set()
    unique_album_links = []
    for link in all_album_links:
        if link not in seen:
            seen.add(link)
            unique_album_links.append(link)
    
    if len(unique_album_links) < len(all_album_links):
        console.print(f"[dim]Removed {len(all_album_links) - len(unique_album_links)} duplicate links[/dim]")
    
    all_album_links = unique_album_links
    
    # Show found albums
    console.print(f"\n[green]✓[/green] Total: {len(all_album_links)} albums:\n")
    table = Table(border_style="dim")
    table.add_column("#", style="cyan", width=4)
    table.add_column("Album URL", style="white")
    
    for i, link in enumerate(all_album_links, 1):
        # Extract album name from URL for display
        album_part = link.split('/album/')[-1] if '/album/' in link else link
        table.add_row(str(i), album_part)
    
    console.print(table)
    console.print()
    
    # Ask user what to do
    console.print("[dim]Options:[/dim]")
    console.print("[dim]  [Enter] Process all albums now[/dim]")
    console.print("[dim]  [S] Save to links.txt for later[/dim]")
    console.print("[dim]  [C] Cancel[/dim]")
    
    choice = Prompt.ask("[cyan]Choice[/cyan]", default="", show_default=False)
    
    if choice.upper() == "C":
        console.print("[yellow]Cancelled[/yellow]")
        return []
    elif choice.upper() == "S":
        # Save to links.txt
        if batch_file:
            with open(batch_file, "a", encoding="utf-8") as f:
                f.write("\n# Parsed from batch parse file\n")
                for link in all_album_links:
                    f.write(link + "\n")
            console.print(f"[green]✓[/green] Saved {len(all_album_links)} links to {batch_file}")
        else:
            console.print("[yellow]No batch file configured[/yellow]")
        # Mark page URLs as processed
        if batch_parse_file and len(page_urls) > 1:
            for url in processed_page_urls:
                mark_link_processed(batch_parse_file, url)
            console.print(f"[green]✓[/green] Marked {len(processed_page_urls)} page URLs as processed")
        return []
    else:
        # Process now - mark page URLs as processed
        if batch_parse_file and len(page_urls) > 1:
            for url in processed_page_urls:
                mark_link_processed(batch_parse_file, url)
            console.print(f"[green]✓[/green] Marked {len(processed_page_urls)} page URLs as processed")
        return all_album_links


def parse_qobuz_page(url):
    """Extract album links from a Qobuz artist/label page."""
    # Validate URL
    if not is_valid_qobuz_url(url):
        console.print("[red]Error:[/red] Not a valid Qobuz URL")
        return []
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract base URL (e.g., "https://www.qobuz.com")
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        
        album_links = []
        seen = set()  # For deduplication
        for container in soup.find_all('div', class_='product__container'):
            link = container.find('a', href=True)
            if link and '/album/' in link['href']:
                full_url = base_url + link['href']
                # Deduplicate
                if full_url not in seen:
                    seen.add(full_url)
                    album_links.append(full_url)
        
        return album_links
    except Exception as e:
        console.print(f"[red]Error:[/red] Could not parse page: {e}")
        return []


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


def get_default_release_type(album_folder, album_name=""):
    """Determine release type based on album name and track count."""
    if album_name:
        album_lower = album_name.lower()
        
        # Check for remix
        if "remix" in album_lower:
            return 13  # Remix
        
        # Check for live album
        if "live" in album_lower:
            return 11  # Live album
        
        # Check for soundtrack
        if "soundtrack" in album_lower or "ost" in album_lower:
            return 3  # Soundtrack
        
        # Check for compilation
        if any(kw in album_lower for kw in ["compilation", "best of", "greatest hits"]):
            return 7  # Compilation
    
    try:
        flac_files = [f for f in os.listdir(album_folder) if f.lower().endswith(".flac")]
        track_count = len(flac_files)
        
        if track_count <= 3:
            return 9   # Single
        elif track_count <= 6:
            return 5   # EP
        else:
            return 1   # Album
    except OSError:
        return 1  # Default to Album


def prompt_release_type(default=1):
    """Prompt user to select a release type."""
    table = Table(title="Release Types", border_style="dim")
    table.add_column("ID", style="cyan", width=4)
    table.add_column("Type", style="white")
    
    for key, value in RELEASE_TYPES.items():
        # Highlight the default
        if key == default:
            table.add_row(str(key), f"[green]{value}[/green] [dim](detected)[/dim]")
        else:
            table.add_row(str(key), value)
    
    console.print()
    console.print(table)
    
    while True:
        try:
            choice = int(Prompt.ask("Select release type", default=str(default)))
            if choice in RELEASE_TYPES:
                return choice
            console.print("[yellow]Invalid choice. Please select a valid release type.[/yellow]")
        except ValueError:
            console.print("[yellow]Please enter a number.[/yellow]")


def get_default_upload_fields(metadata, qobuz_url=None, album_folder=None):
    """Generate upload fields with all defaults/parsed values without prompting."""
    # Detect release type based on album name and track count
    default_release_type = get_default_release_type(album_folder, metadata.get("album", "")) if album_folder else 1
    
    # Derive defaults from metadata
    bitrate = get_bitrate_string(metadata["bits_per_sample"])
    release_desc = get_release_description(
        metadata["bits_per_sample"], 
        metadata["sample_rate"],
        qobuz_url
    )
    
    # Try to get album cover and tracklist from Qobuz
    album_cover = get_qobuz_cover(qobuz_url)
    tracklist = get_qobuz_tracklist(qobuz_url)
    
    fields = {
        "type": 0,  # Music
        "artists[]": metadata.get("artist", ""),
        "importance[]": 1,  # Main
        "title": metadata.get("album", ""),
        "year": metadata.get("year", ""),
        "releasetype": default_release_type,
        "unknown": "0",
        "remaster_year": metadata.get("year", ""),
        "remaster_title": "",
        "remaster_record_label": metadata.get("label", ""),
        "remaster_catalogue_number": "",
        "scene": "0",
        "format": "FLAC",
        "bitrate": bitrate,
        "media": "WEB",
        "tags": metadata.get("genre", ""),
        "image": album_cover or "",
        "album_desc": tracklist or "",
        "release_desc": release_desc,
    }
    
    return fields


def prompt_upload_fields(metadata, qobuz_url=None, album_folder=None):
    """Prompt user to confirm/edit all upload fields."""
    console.print()
    console.print(Panel("[bold]UPLOAD DETAILS[/bold] - Confirm or edit each field", border_style="cyan"))
    console.print()
    
    # Detect release type based on album name and track count
    default_release_type = get_default_release_type(album_folder, metadata.get("album", "")) if album_folder else 1
    
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
    
    # Release type (auto-detected based on track count)
    fields["releasetype"] = prompt_release_type(default_release_type)
    
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
    batch_file = config.get("batch_file")
    batch_parse_file = config.get("batch_parse")
    
    # Main loop - process albums until user exits
    while True:
        console.print("\n" + "─" * 50)
        
        try:
            # Check for existing albums in destination (sorted by date, newest first, max 10)
            existing_albums = get_existing_folders(destination_dir)
            
            # Check for batch links
            batch_links = read_batch_links(batch_file)
            
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
                if batch_links:
                    console.print(f"[dim][B] Batch process from links.txt ({len(batch_links)} links found)[/dim]")
                console.print("[dim][P] Parse Qobuz artist/label page[/dim]")
                
                use_existing = Prompt.ask(
                    "[cyan]Selection[/cyan]",
                    default="",
                    show_default=False
                )
                
                # Check for parse mode
                if use_existing.upper() == "P":
                    batch_links = handle_parse_qobuz_page(batch_file, batch_parse_file)
                    if batch_links:
                        use_existing = None
                    else:
                        continue
                # Check for batch mode
                elif use_existing.upper() == "B" and batch_links:
                    use_existing = None
                    # Process batch - will be handled below
                    pass
                elif use_existing.upper() == "B":
                    console.print("[yellow]No batch links found in links.txt[/yellow]")
                    continue
                elif use_existing:
                    try:
                        album_index = int(use_existing) - 1
                        album_name = recent_albums[album_index]
                        final_path = os.path.join(destination_dir, album_name)
                        console.print(f"\n[green]✓[/green] Using existing album: [bold]{final_path}[/bold]")
                        batch_links = []  # Clear batch links for single mode
                    except (ValueError, IndexError):
                        console.print("[yellow]Invalid selection, proceeding with download...[/yellow]")
                        use_existing = None
                        batch_links = []  # Clear batch links for single mode
                else:
                    batch_links = []  # Clear batch links for single mode
            else:
                use_existing = None
                # Show options even without existing albums
                if batch_links:
                    console.print(f"\n[dim][B] Batch process from links.txt ({len(batch_links)} links found)[/dim]")
                console.print("[dim][P] Parse Qobuz artist/label page[/dim]")
                selection = Prompt.ask(
                    "[cyan]Press B for batch, P to parse page, or Enter to download new[/cyan]",
                    default="",
                    show_default=False
                )
                if selection.upper() == "P":
                    batch_links = handle_parse_qobuz_page(batch_file, batch_parse_file)
                    if not batch_links:
                        continue
                elif selection.upper() == "B" and batch_links:
                    pass  # batch_links already set
                else:
                    batch_links = []  # Clear for single mode
            
            # Batch processing mode
            if batch_links:
                console.print(Panel(f"[bold]BATCH MODE: Processing {len(batch_links)} albums[/bold]", border_style="cyan"))
                
                # Ask for processing mode once for all albums
                console.print()
                console.print("[dim]Processing mode:[/dim]")
                console.print("[dim]  [Enter] Manual (review each album)[/dim]")
                console.print("[dim]  [A] Automatic (use defaults, skip all prompts for all albums)[/dim]")
                mode_choice = Prompt.ask("[cyan]Mode[/cyan]", default="", show_default=False)
                auto_mode = (mode_choice.upper() == "A")
                
                if auto_mode:
                    console.print("[green]✓[/green] Automatic mode enabled - processing all albums with defaults")
                
                successful = 0
                failed = 0
                
                for idx, batch_url in enumerate(batch_links, 1):
                    console.print()
                    console.print(Panel(f"[bold cyan]Batch {idx}/{len(batch_links)}[/bold cyan]\n{batch_url}", border_style="blue"))
                    
                    try:
                        # Download
                        console.print("\n[cyan]⬇[/cyan]  Downloading album...")
                        album_folder = download_album(batch_url, download_dir)
                        
                        if not album_folder:
                            console.print("[red]Error:[/red] Could not detect downloaded album folder.")
                            failed += 1
                            continue
                        
                        # Flatten any nested folders caused by backslashes in metadata
                        album_folder = flatten_nested_album_folder(album_folder)
                        
                        console.print(f"[green]✓[/green] Downloaded to: [dim]{album_folder}[/dim]")
                        
                        # Recompress
                        console.print("\n[cyan]🔄[/cyan] Recompressing FLAC files...")
                        recompress_flac_files(album_folder, flac_path)
                        console.print("[green]✓[/green] Recompression complete.")
                        
                        # Move
                        console.print(f"\n[cyan]📁[/cyan] Moving album to destination...")
                        final_path = move_album(album_folder, destination_dir)
                        console.print(f"[green]✓[/green] Album moved to: [dim]{final_path}[/dim]")
                        
                        # Create torrent
                        album_name = os.path.basename(final_path)
                        expected_torrent = os.path.join(torrent_output_dir, f"{album_name}.torrent")
                        
                        if os.path.exists(expected_torrent):
                            console.print(f"[green]✓[/green] Using existing torrent")
                            torrent_path = expected_torrent
                        else:
                            console.print("\n[cyan]📦[/cyan] Creating torrent file...")
                            torrent_path = create_torrent(final_path, announce_url, torrent_output_dir)
                            console.print(f"[green]✓[/green] Torrent created: [dim]{torrent_path}[/dim]")
                        
                        # Read metadata
                        console.print("\n[cyan]🔍[/cyan] Reading FLAC metadata...")
                        metadata = read_flac_metadata(final_path)
                        
                        if not metadata:
                            console.print("[yellow]Warning:[/yellow] Could not read FLAC metadata.")
                            metadata = {
                                "artist": "",
                                "album": "",
                                "year": "",
                                "label": "",
                                "genre": "",
                                "bits_per_sample": 16,
                                "sample_rate": 44100,
                            }
                        
                        # Prompt for upload fields (user confirms each upload)
                        if auto_mode:
                            # Automatic: use defaults, no prompts
                            console.print("[cyan]🔍[/cyan] Fetching album info from Qobuz...")
                            upload_fields = get_default_upload_fields(metadata, batch_url, final_path)
                            console.print("[green]✓[/green] Using default values, auto-uploading...")
                        else:
                            # Manual: show prompt
                            console.print()
                            console.print("[dim]Options:[/dim]")
                            console.print("[dim]  [Enter] Review and edit fields[/dim]")
                            console.print("[dim]  [U] Use defaults and upload[/dim]")
                            choice = Prompt.ask("[cyan]Choice[/cyan]", default="", show_default=False)
                            
                            if choice.upper() == "U":
                                # Use defaults without prompting
                                console.print("[cyan]🔍[/cyan] Fetching album info from Qobuz...")
                                upload_fields = get_default_upload_fields(metadata, batch_url, final_path)
                                console.print("[green]✓[/green] Using default values for all fields")
                            else:
                                # Normal flow with prompts
                                upload_fields = prompt_upload_fields(metadata, batch_url, final_path)
                        
                        # Ask to proceed with upload (skip if auto mode)
                        if not auto_mode:
                            console.print()
                            if not Confirm.ask("[bold cyan]Proceed with upload?[/bold cyan]", default=True):
                                console.print("[yellow]Skipped.[/yellow]")
                                failed += 1
                                continue
                        
                        # Upload
                        console.print("\n[cyan]⬆[/cyan]  Uploading to RED...")
                        upload_result = upload_torrent(torrent_path, upload_fields, api_key, dry_run=False, debug=debug)
                        
                        if upload_result.get("status") == "success":
                            response = upload_result.get("response", {})
                            console.print(Panel.fit(
                                f"[bold green]✓ Upload Successful![/bold green]\n\n"
                                f"[white]Torrent ID:[/white] [cyan]{response.get('torrentid')}[/cyan]\n"
                                f"[white]Group ID:[/white] [cyan]{response.get('groupid')}[/cyan]",
                                border_style="green"
                            ))
                            
                            # Move torrent to watch folder
                            if watch_folder:
                                if not os.path.exists(watch_folder):
                                    os.makedirs(watch_folder)
                                torrent_filename = os.path.basename(torrent_path)
                                watch_path = os.path.join(watch_folder, torrent_filename)
                                shutil.move(torrent_path, watch_path)
                                console.print(f"[green]✓[/green] Torrent moved to: [dim]{watch_path}[/dim]")
                            
                            # Mark link as processed
                            mark_link_processed(batch_file, batch_url)
                            successful += 1
                        else:
                            console.print(f"[red]✗ Upload failed:[/red] {upload_result.get('error', 'Unknown error')}")
                            failed += 1
                    
                    except Exception as e:
                        console.print(f"[red]✗ Error processing album:[/red] {e}")
                        failed += 1
                        continue
                
                # Batch summary
                console.print()
                console.print(Panel(
                    f"[bold]Batch Complete![/bold]\n\n"
                    f"[green]✓ Successful:[/green] {successful}\n"
                    f"[red]✗ Failed:[/red] {failed}",
                    border_style="cyan"
                ))
                
                # Ask if user wants to continue
                console.print()
                if not Confirm.ask("[cyan]Process another batch or album?[/cyan]", default=True):
                    console.print("\n[dim]Goodbye![/dim]")
                    break
                continue
            
            if not use_existing:
                url = Prompt.ask("[cyan]Enter Qobuz album URL[/cyan]")
                
                if not url:
                    console.print("[red]Error:[/red] No URL provided.")
                    continue
            else:
                # Ask for URL for release description when using existing album
                url = Prompt.ask("[cyan]Enter Qobuz album URL[/cyan] [dim](for metadata, optional)[/dim]", default="") or None
            
            if not use_existing:
                console.print("\n[cyan]⬇[/cyan]  Downloading album...")
                album_folder = download_album(url, download_dir)
                
                if not album_folder:
                    console.print("[red]Error:[/red] Could not detect downloaded album folder.")
                    continue
                
                # Flatten any nested folders caused by backslashes in metadata
                album_folder = flatten_nested_album_folder(album_folder)
                
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
                console.print("[yellow]Warning:[/yellow] Could not read FLAC metadata. You'll need to enter details manually.")
                metadata = {
                    "artist": "",
                    "album": "",
                    "year": "",
                    "label": "",
                    "genre": "",
                    "bits_per_sample": 16,
                    "sample_rate": 44100,
                }
            
            # Prompt for upload fields
            console.print()
            console.print("[dim]Options:[/dim]")
            console.print("[dim]  [Enter] Review and edit fields[/dim]")
            console.print("[dim]  [U] Use defaults and upload[/dim]")
            console.print("[dim]  [A] Automatic (use defaults, skip all prompts)[/dim]")
            choice = Prompt.ask("[cyan]Choice[/cyan]", default="", show_default=False)
            
            auto_upload = (choice.upper() == "A")
            
            if choice.upper() == "U" or auto_upload:
                # Use defaults without prompting
                console.print("[cyan]🔍[/cyan] Fetching album info from Qobuz...")
                upload_fields = get_default_upload_fields(metadata, url, final_path)
                if auto_upload:
                    console.print("[green]✓[/green] Using default values, auto-uploading...")
                else:
                    console.print("[green]✓[/green] Using default values for all fields")
            else:
                # Normal flow with prompts
                upload_fields = prompt_upload_fields(metadata, url, final_path)
            
            # Ask if user wants to do a dry run first (skip if auto mode)
            if not auto_upload:
                console.print()
                do_dry_run = Confirm.ask("[cyan]Do dry run first?[/cyan]", default=False)
                
                if do_dry_run:
                    console.print()
                    console.print(Panel("[bold]PERFORMING DRY RUN...[/bold]", border_style="yellow"))
                    
                    dry_run_result = upload_torrent(torrent_path, upload_fields, api_key, dry_run=True, debug=debug)
                    
                    console.print("\n[bold]Dry run result:[/bold]")
                    console.print_json(json.dumps(dry_run_result, indent=2))
                    
                    # Check for success - API returns "success" or "dry run success"
                    status = dry_run_result.get("status", "")
                    if "success" not in status.lower():
                        console.print(f"\n[red]✗ Dry run failed:[/red] {dry_run_result.get('error', 'Unknown error')}")
                        continue
                    else:
                        console.print("\n[green]✓ Dry run successful![/green]")
            else:
                do_dry_run = False
            
            # Ask to proceed with actual upload (skip if auto mode)
            if not auto_upload:
                console.print()
                proceed = Confirm.ask("[bold cyan]Proceed with actual upload?[/bold cyan]", default=True)
            else:
                proceed = True
            
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
                    continue
            else:
                console.print("\n[yellow]Upload cancelled.[/yellow]")
            
            console.print("\n[bold green]Done![/bold green]")
            
            # Ask if user wants to process another album
            console.print()
            if not Confirm.ask("[cyan]Process another album?[/cyan]", default=True):
                console.print("\n[dim]Goodbye![/dim]")
                break
                
        except subprocess.CalledProcessError as e:
            console.print(f"\n[red]✗ Error:[/red] Command failed: {e}")
            if not Confirm.ask("[cyan]Try another album?[/cyan]", default=True):
                sys.exit(1)
        except KeyboardInterrupt:
            console.print("\n\n[dim]Interrupted. Goodbye![/dim]")
            sys.exit(0)
        except Exception as e:
            console.print(f"\n[red]✗ Error:[/red] {e}")
            if not Confirm.ask("[cyan]Try another album?[/cyan]", default=True):
                sys.exit(1)


if __name__ == "__main__":
    main()
