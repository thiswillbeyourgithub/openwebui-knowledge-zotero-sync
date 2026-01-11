#!/usr/bin/env python3
"""Sync local directory files with OpenWebUI knowledge base.

This script provides a CLI tool to synchronize files from a local directory
to an OpenWebUI knowledge base. Files are tracked using modification timestamps
to detect changes, and filenames are encoded with a directory identifier to
support multiple sync directories.

This tool was developed with assistance from aider.chat.
"""

import json
import mimetypes
import pdb
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

import click
import requests
from loguru import logger

from utils.datatypes import File, KnowledgeBase, validate_response

# Configure logger to write to both console and file
# Detailed logs go to file, INFO+ goes to console
logger.remove()  # Remove default handler
logger.add(sys.stderr, level="INFO", format="<level>{message}</level>")
logger.add("log.txt", rotation="10 MB", retention="10 days", level="DEBUG")


def get_file_mtime(filepath: Path) -> int:
    """Get file modification time as Unix timestamp.

    Parameters
    ----------
    filepath : Path
        Path to the file

    Returns
    -------
    int
        Unix timestamp (seconds since epoch) of last modification
    """
    return int(filepath.stat().st_mtime)


def encode_filename(filepath: str, kbdir_id: str) -> str:
    """Encode filename with kbdir_id prefix and %% separators.

    This encoding allows multiple sync directories to coexist in the same
    knowledge base by prefixing each file with a unique directory identifier.
    Path separators (/ and \\) are encoded as %% to create a flat namespace.

    Parameters
    ----------
    filepath : str
        Relative file path to encode
    kbdir_id : str
        Knowledge base directory identifier

    Returns
    -------
    str
        Encoded filename: kbdir_id%%path%%to%%file
    """
    # Normalize path separators to forward slash
    normalized = filepath.replace("\\", "/")
    # Replace forward slashes with %%
    encoded_path = normalized.replace("/", "%%")
    return f"{kbdir_id}%%{encoded_path}"


def decode_filename(encoded_name: str, kbdir_id: str) -> Optional[str]:
    """Decode filename and verify it belongs to the specified kbdir_id.

    Parameters
    ----------
    encoded_name : str
        Encoded filename to decode
    kbdir_id : str
        Expected knowledge base directory identifier

    Returns
    -------
    Optional[str]
        Decoded filename if it belongs to kbdir_id, None otherwise
    """
    prefix = f"{kbdir_id}%%"
    if not encoded_name.startswith(prefix):
        return None
    # Remove prefix and convert %% back to /
    return encoded_name[len(prefix) :].replace("%%", "/")


def get_local_files(directory: Path, file_regex: Optional[str] = None) -> List[str]:
    """Recursively find files in directory matching optional regex pattern.

    Excludes hidden files (starting with .) and the log file.

    Parameters
    ----------
    directory : Path
        Root directory to search
    file_regex : Optional[str]
        Regular expression pattern to filter files. If None, all files match.

    Returns
    -------
    List[str]
        List of file paths relative to directory
    """
    files = []
    pattern = re.compile(file_regex) if file_regex else None

    for item in directory.rglob("*"):
        # Skip directories, hidden files, and the log file
        if item.is_dir():
            continue
        if item.name.startswith("."):
            continue
        if item.name == "log.txt":
            continue

        # Get path relative to sync directory
        try:
            relative_path = item.relative_to(directory)
        except ValueError:
            continue

        relative_str = str(relative_path.as_posix())

        # Apply regex filter if provided
        if pattern and not pattern.search(relative_str):
            continue

        files.append(relative_str)

    return sorted(files)


def make_request(
    method: str,
    endpoint: str,
    base_url: str,
    api_key: str,
    json_data: Optional[Dict] = None,
    files: Optional[Dict] = None,
    stream: bool = False,
) -> requests.Response:
    """Make HTTP request to OpenWebUI API with proper authentication.

    Parameters
    ----------
    method : str
        HTTP method (GET, POST, DELETE)
    endpoint : str
        API endpoint path (e.g., '/api/v1/files/')
    base_url : str
        Base API URL
    api_key : str
        Authentication API key
    json_data : Optional[Dict]
        JSON payload for request body
    files : Optional[Dict]
        Files dictionary for multipart upload
    stream : bool
        Whether to stream the response

    Returns
    -------
    requests.Response
        HTTP response object

    Raises
    ------
    requests.exceptions.HTTPError
        If the request fails with a non-2xx status code
    """
    url = f"{base_url.rstrip('/')}{endpoint}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    logger.debug(f"{method} {url}")
    if json_data:
        logger.debug(f"Request body: {json.dumps(json_data, indent=2)}")

    response = requests.request(
        method=method,
        url=url,
        headers=headers,
        json=json_data,
        files=files,
        stream=stream,
    )

    logger.debug(f"Response status: {response.status_code}")
    response.raise_for_status()

    return response


def upload_file(
    filepath: Path, relative_path: str, kbdir_id: str, base_url: str, api_key: str
) -> Dict:
    """Upload file to OpenWebUI with encoded filename.

    The filename is encoded with the kbdir_id prefix to support multiple
    sync directories in the same OpenWebUI instance.

    Parameters
    ----------
    filepath : Path
        Absolute path to the file to upload
    relative_path : str
        File path relative to sync directory
    kbdir_id : str
        Knowledge base directory identifier
    base_url : str
        Base API URL
    api_key : str
        Authentication API key

    Returns
    -------
    Dict
        Upload response containing file metadata including 'id' and 'hash'

    Raises
    ------
    requests.exceptions.HTTPError
        If upload fails
    """
    encoded_name = encode_filename(relative_path, kbdir_id)
    logger.debug(f"Uploading {relative_path} as {encoded_name}")

    # Detect MIME type based on file extension
    content_type, _ = mimetypes.guess_type(str(filepath))

    logger.debug(f"Detected content type: {content_type}")

    # Read file content into memory to ensure it's fully available for upload
    # Some APIs have issues with streaming file handles in multipart uploads
    with open(filepath, "rb") as f:
        file_content = f.read()

    if content_type is None:
        files = {"file": (encoded_name, file_content)}
    else:
        files = {"file": (encoded_name, file_content, content_type)}
    response = make_request(
        method="POST",
        endpoint="/api/v1/files/",
        base_url=base_url,
        api_key=api_key,
        files=files,
    )

    return response.json()


def add_file_to_kb(file_id: str, kb_id: str, base_url: str, api_key: str) -> Dict:
    """Add an uploaded file to a knowledge base.

    Parameters
    ----------
    file_id : str
        ID of the uploaded file
    kb_id : str
        Knowledge base ID
    base_url : str
        Base API URL
    api_key : str
        Authentication API key

    Returns
    -------
    Dict
        Knowledge base metadata after adding the file

    Raises
    ------
    requests.exceptions.HTTPError
        If add operation fails
    """
    response = make_request(
        method="POST",
        endpoint=f"/api/v1/knowledge/{kb_id}/file/add",
        base_url=base_url,
        api_key=api_key,
        json_data={"file_id": file_id},
    )
    return response.json()


def remove_file_from_kb(file_id: str, kb_id: str, base_url: str, api_key: str) -> Dict:
    """Remove a file from a knowledge base.

    This also deletes the file from storage automatically per OpenWebUI behavior.

    Parameters
    ----------
    file_id : str
        ID of the file to remove
    kb_id : str
        Knowledge base ID
    base_url : str
        Base API URL
    api_key : str
        Authentication API key

    Returns
    -------
    Dict
        Knowledge base metadata after removing the file

    Raises
    ------
    requests.exceptions.HTTPError
        If remove operation fails
    """
    response = make_request(
        method="POST",
        endpoint=f"/api/v1/knowledge/{kb_id}/file/remove",
        base_url=base_url,
        api_key=api_key,
        json_data={"file_id": file_id},
    )
    return response.json()


def resolve_kb_id(
    kb_id: Optional[str], kb_name: Optional[str], base_url: str, api_key: str
) -> str:
    """Resolve knowledge base name to ID, or return ID if provided.

    Exactly one of kb_id or kb_name must be provided. If kb_name is given,
    it will be looked up via the API to find the corresponding ID.

    Parameters
    ----------
    kb_id : Optional[str]
        Knowledge base ID (if provided directly)
    kb_name : Optional[str]
        Knowledge base name to resolve to ID
    base_url : str
        Base API URL
    api_key : str
        Authentication API key

    Returns
    -------
    str
        Knowledge base ID

    Raises
    ------
    click.ClickException
        If neither kb_id nor kb_name is provided, both are provided,
        or if kb_name doesn't match any knowledge base
    """
    if kb_id and kb_name:
        raise click.ClickException(
            "Cannot specify both --kb-id and --kb-name, choose one"
        )

    if not kb_id and not kb_name:
        raise click.ClickException("Either --kb-id or --kb-name must be provided")

    if kb_id:
        return kb_id

    # Fetch all knowledge bases to find matching name
    logger.debug(f"Resolving knowledge base name '{kb_name}' to ID...")
    response = make_request(
        method="GET",
        endpoint="/api/v1/knowledge/",
        base_url=base_url,
        api_key=api_key,
    )
    kb_list = response.json()
    if "items" in kb_list and "total" in kb_list:
        kb_list = kb_list["items"]

    # Find KB by name
    for kb in kb_list:
        if kb.get("name") == kb_name:
            resolved_id = kb.get("id")
            logger.info(f"Resolved knowledge base '{kb_name}' to ID: {resolved_id}")
            return resolved_id

    # No match found
    available_names = [kb.get("name") for kb in kb_list if kb.get("name")]
    raise click.ClickException(
        f"Knowledge base '{kb_name}' not found. Available: {', '.join(available_names)}"
    )


def sync_directory(
    directory: Path,
    kb_id: str,
    kbdir_id: str,
    base_url: str,
    api_key: str,
    file_regex: Optional[str] = None,
    dry: bool = False,
) -> None:
    """Synchronize directory contents with OpenWebUI knowledge base.

    Sync logic follows a two-phase approach:
    1. Delete phase: Remove files from KB that don't exist locally or are outdated
    2. Upload phase: Upload and add new or changed files to KB

    Files are compared using modification timestamps - local file mtime is compared
    with remote file updated_at to determine if re-upload is needed.

    Parameters
    ----------
    directory : Path
        Local directory to sync
    kb_id : str
        Knowledge base ID
    kbdir_id : str
        Knowledge base directory identifier for file naming
    base_url : str
        Base API URL
    api_key : str
        Authentication API key
    file_regex : Optional[str]
        Regular expression to filter files for syncing
    dry : bool
        If True, show what would be done without making changes

    Raises
    ------
    requests.exceptions.HTTPError
        If any API operation fails
    """
    if dry:
        logger.info(f"[DRY RUN] Starting synchronization preview of {directory}")
    else:
        logger.info(f"Starting synchronization of {directory}")

    # Step 1: Build local file inventory with modification times
    logger.info("Scanning local files...")
    local_files = get_local_files(directory, file_regex)
    local_mtimes: Dict[str, int] = {}

    for rel_path in local_files:
        abs_path = directory / rel_path
        mtime = get_file_mtime(abs_path)
        local_mtimes[rel_path] = mtime
        logger.debug(f"Local file: {rel_path} -> mtime={mtime}")

    logger.info(f"Found {len(local_files)} local files")

    # Step 2: Get current knowledge base state
    logger.info(f"Fetching knowledge base {kb_id}...")
    kb_response = make_request(
        method="GET",
        endpoint=f"/api/v1/knowledge/{kb_id}",
        base_url=base_url,
        api_key=api_key,
    )
    kb_data_raw = kb_response.json()

    # Validate knowledge base response
    kb_validated = validate_response(
        kb_data_raw, KnowledgeBase, f"knowledge base {kb_id}"
    )
    if kb_validated:
        kb_files = kb_validated.files or []
    else:
        # Fallback to raw data if validation fails
        logger.warning("Using raw knowledge base data due to validation failure")
        kb_files = kb_data_raw.get("files", [])

    if kb_files is None:
        kb_files = []

    logger.info(f"Knowledge base contains {len(kb_files)} files")

    # Step 3: Get all files to build hash map and reuse map
    logger.info("Fetching all uploaded files for hash mapping...")
    all_files_response = make_request(
        method="GET", endpoint="/api/v1/files/", base_url=base_url, api_key=api_key
    )
    all_files_raw = all_files_response.json()

    # Validate each file in the response
    all_files = []
    for file_data in all_files_raw:
        file_validated = validate_response(file_data, File, "file in all files list")
        if file_validated:
            all_files.append(file_validated.model_dump())
        else:
            # Keep raw data if validation fails
            logger.warning("Using raw file data due to validation failure")
            all_files.append(file_data)

    # Build maps for efficient lookup
    # Map decoded filename -> updated_at timestamp for files belonging to our kbdir_id
    file_updated_at_map: Dict[str, int] = {}
    # Map decoded filename -> file_id for files belonging to our kbdir_id
    file_id_by_name: Dict[str, str] = {}

    for file_info in all_files:
        encoded_name = file_info.get("meta", {}).get("name", "")
        decoded_name = decode_filename(encoded_name, kbdir_id)

        if decoded_name is not None:
            # This file belongs to our sync directory
            updated_at = file_info.get("updated_at")
            file_id = file_info.get("id")

            if updated_at:
                file_updated_at_map[decoded_name] = updated_at
            if file_id:
                file_id_by_name[decoded_name] = file_id

            logger.debug(
                f"Remote file: {decoded_name} -> {file_id} (updated_at: {updated_at})"
            )

    # Step 4: Delete files from KB that don't match local state
    logger.info("Checking for files to delete from knowledge base...")
    deleted_count = 0

    for kb_file_data in kb_files:
        # Handle both validated File objects and raw dicts
        if isinstance(kb_file_data, File):
            kb_file = kb_file_data.model_dump()
        else:
            kb_file = kb_file_data

        encoded_name = kb_file.get("meta", {}).get("name", "")
        decoded_name = decode_filename(encoded_name, kbdir_id)

        if decoded_name is None:
            # Not from our sync directory, skip
            logger.debug(f"Skipping non-directory file: {encoded_name}")
            continue

        local_mtime = local_mtimes.get(decoded_name)
        remote_updated_at = file_updated_at_map.get(decoded_name)

        if decoded_name not in local_files:
            # File no longer exists locally
            if dry:
                logger.info(
                    f"[DRY RUN] Would delete (no longer exists locally): {decoded_name}"
                )
            else:
                logger.info(f"Deleting (no longer exists locally): {decoded_name}")
                remove_file_from_kb(kb_file["id"], kb_id, base_url, api_key)
            deleted_count += 1
        elif local_mtime and remote_updated_at and local_mtime > remote_updated_at:
            # Local file is newer than remote
            if dry:
                logger.info(
                    f"[DRY RUN] Would delete (local file is newer): {decoded_name}"
                )
            else:
                logger.info(f"Deleting (local file is newer): {decoded_name}")
            logger.info(f"  Local mtime:        {local_mtime}")
            logger.info(f"  Remote updated_at:  {remote_updated_at}")
            if not dry:
                remove_file_from_kb(kb_file["id"], kb_id, base_url, api_key)
            deleted_count += 1
        else:
            # File unchanged or remote is newer
            logger.debug(f"Keeping (up to date): {decoded_name}")

    if dry:
        logger.info(f"[DRY RUN] Would delete {deleted_count} files from knowledge base")
    else:
        logger.info(f"Deleted {deleted_count} files from knowledge base")

    # Step 5: Upload and add new or changed files
    logger.info("Checking for files to add or update...")
    uploaded_count = 0
    added_count = 0
    failed_files = []  # Track files that failed to upload or add

    for rel_path in local_files:
        local_mtime = local_mtimes[rel_path]
        remote_updated_at = file_updated_at_map.get(rel_path)

        # Check if file is already in KB with current content
        file_in_kb = any(
            decode_filename(f.get("meta", {}).get("name", ""), kbdir_id) == rel_path
            for f in kb_files
        )

        if file_in_kb and remote_updated_at and local_mtime <= remote_updated_at:
            # Remote file is up to date or newer than local
            logger.debug(f"Skipping (up to date in KB): {rel_path}")
            continue

        # File needs to be uploaded
        if dry:
            logger.info(f"[DRY RUN] Would upload: {rel_path}")
            uploaded_count += 1
            # In dry run, we can't get a real file_id, so skip the add step
            continue
        else:
            logger.info(f"Uploading: {rel_path}")
            abs_path = directory / rel_path
            upload_result = upload_file(abs_path, rel_path, kbdir_id, base_url, api_key)

            if not upload_result.get("id"):
                logger.error(f"Upload failed for {rel_path}: No file ID in response")
                logger.error(f"Response: {json.dumps(upload_result, indent=2)}")
                failed_files.append((rel_path, "upload failed - no file ID"))
                continue

            file_id = upload_result["id"]
            logger.info(f"Uploaded successfully: {rel_path} ({file_id})")
            uploaded_count += 1

        # Add file to knowledge base
        if dry:
            logger.info(f"[DRY RUN] Would add to knowledge base: {rel_path}")
            added_count += 1
        else:
            logger.info(f"Adding to knowledge base: {rel_path}")
            try:
                add_result = add_file_to_kb(file_id, kb_id, base_url, api_key)

                if not add_result.get("id"):
                    logger.error(f"Failed to add {rel_path} to knowledge base")
                    logger.error(f"Response: {json.dumps(add_result, indent=2)}")
                    failed_files.append((rel_path, "add to KB failed - no KB ID"))
                    continue

                logger.info(f"Added to KB successfully: {rel_path}")
                added_count += 1

            except requests.exceptions.HTTPError as e:
                logger.error(f"Failed to add {rel_path} to knowledge base: {e}")
                failed_files.append((rel_path, f"add to KB failed - {e}"))
                continue

    # Check for failures and raise exception if any occurred
    if failed_files and not dry:
        error_summary = "\n".join(
            f"  - {path}: {reason}" for path, reason in failed_files
        )
        raise click.ClickException(
            f"Sync completed with {len(failed_files)} failures:\n{error_summary}"
        )

    if dry:
        logger.info(
            f"[DRY RUN] Summary: {uploaded_count} would be uploaded, {added_count} would be added to KB"
        )
        logger.info("[DRY RUN] Synchronization preview completed!")
    else:
        logger.info(
            f"Upload summary: {uploaded_count} uploaded, {added_count} added to KB"
        )
        logger.info("Synchronization completed successfully!")


@click.group()
def cli():
    """OpenWebUI Knowledge Base Sync Tool.

    This tool synchronizes local directory files with an OpenWebUI knowledge base.
    Configuration can be provided via command-line options or environment variables
    with OPENWEBUI_ prefix.
    """
    pass


@cli.command()
@click.option(
    "--base-url",
    envvar="OPENWEBUI_BASE_URL",
    default="http://localhost:3000",
    help="OpenWebUI API base URL",
)
@click.option(
    "--api-key",
    envvar="OPENWEBUI_API_KEY",
    required=True,
    help="OpenWebUI API authentication key",
)
@click.option(
    "--kb-id",
    envvar="OPENWEBUI_KB_ID",
    help="Knowledge base ID to sync with",
)
@click.option(
    "--kb-name",
    envvar="OPENWEBUI_KB_NAME",
    help="Knowledge base name to sync with (alternative to --kb-id)",
)
@click.option(
    "--kbdir-id",
    envvar="OPENWEBUI_KBDIR_ID",
    required=True,
    help="Unique identifier for this sync directory (used in filename encoding)",
)
@click.option(
    "--file-regex",
    envvar="OPENWEBUI_FILE_REGEX",
    help="Regular expression to filter files (e.g., '.*\\.md$' for markdown only)",
)
@click.option(
    "--dry",
    is_flag=True,
    help="Dry run - show what would be done without making changes",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug mode - drop into pdb debugger on exceptions",
)
@click.argument(
    "directory",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=".",
)
def sync(
    base_url, api_key, kb_id, kb_name, kbdir_id, file_regex, dry, debug, directory
):
    """Synchronize DIRECTORY with OpenWebUI knowledge base.

    All files in the knowledge base belonging to this kbdir-id that don't exist
    locally or have different content will be deleted. All local files will be
    uploaded or updated as needed.

    Specify the knowledge base using either --kb-id or --kb-name.
    """
    try:
        # Resolve knowledge base name to ID if needed
        resolved_kb_id = resolve_kb_id(kb_id, kb_name, base_url, api_key)

        sync_directory(
            directory=directory,
            kb_id=resolved_kb_id,
            kbdir_id=kbdir_id,
            base_url=base_url,
            api_key=api_key,
            file_regex=file_regex,
            dry=dry,
        )
    except Exception:
        if debug:
            logger.exception("Exception occurred during sync:")
            logger.error("Entering debugger...")
            pdb.post_mortem()
        raise


@cli.command()
@click.option(
    "--base-url",
    envvar="OPENWEBUI_BASE_URL",
    default="http://localhost:3000",
    help="OpenWebUI API base URL",
)
@click.option(
    "--api-key",
    envvar="OPENWEBUI_API_KEY",
    required=True,
    help="OpenWebUI API authentication key",
)
@click.option(
    "--full",
    is_flag=True,
    help="Return full knowledge base data instead of simplified output",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug mode - drop into pdb debugger on exceptions",
)
def list_kb(base_url, api_key, full, debug):
    """List all knowledge bases."""
    try:
        response = make_request(
            method="GET",
            endpoint="/api/v1/knowledge/",
            base_url=base_url,
            api_key=api_key,
        )
        kb_list = response.json()
        if "items" in kb_list and "total" in kb_list:
            kb_list = kb_list["items"]

        # Simplify output to show only essential fields, unless --full is specified
        if full:
            output = kb_list
        else:
            output = [
                {
                    "id": kb.get("id"),
                    "name": kb.get("name"),
                    "description": kb.get("description"),
                }
                for kb in kb_list
            ]

        print(json.dumps(output, indent=2))
    except Exception:
        if debug:
            logger.error("Exception occurred, entering debugger...")
            pdb.post_mortem()
        raise


@cli.command()
@click.option(
    "--base-url",
    envvar="OPENWEBUI_BASE_URL",
    default="http://localhost:3000",
    help="OpenWebUI API base URL",
)
@click.option(
    "--api-key",
    envvar="OPENWEBUI_API_KEY",
    required=True,
    help="OpenWebUI API authentication key",
)
@click.option(
    "--full",
    is_flag=True,
    help="Return full file data instead of redacted output",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug mode - drop into pdb debugger on exceptions",
)
def list_files(base_url, api_key, full, debug):
    """List all uploaded files."""
    try:
        response = make_request(
            method="GET", endpoint="/api/v1/files/", base_url=base_url, api_key=api_key
        )
        files = response.json()

        # Truncate data field by default unless --full is specified
        if not full:
            for file_info in files:
                if "data" in file_info:
                    if "content" in file_info["data"]:
                        content = file_info["data"]["content"]
                        if isinstance(content, str) and len(content) > 100:
                            file_info["data"]["content"] = content[:100] + "[TRUNCATED]"
                    else:
                        data_str = str(file_info["data"])
                        if len(data_str) > 100:
                            file_info["data"] = data_str[:100] + "[TRUNCATED]"

        print(json.dumps(files, indent=2))
    except Exception:
        if debug:
            logger.error("Exception occurred, entering debugger...")
            pdb.post_mortem()
        raise


@cli.command()
@click.option(
    "--base-url",
    envvar="OPENWEBUI_BASE_URL",
    default="http://localhost:3000",
    help="OpenWebUI API base URL",
)
@click.option(
    "--api-key",
    envvar="OPENWEBUI_API_KEY",
    required=True,
    help="OpenWebUI API authentication key",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug mode - drop into pdb debugger on exceptions",
)
def files_status(base_url, api_key, debug):
    """List files with non-completed status.

    Returns a dict with filename as key and data (excluding content) as value
    for all files where status is not "completed". Includes collection name
    if the file is in a knowledge base.
    """
    try:
        # Fetch all knowledge bases to map file IDs to collection names
        kb_response = make_request(
            method="GET",
            endpoint="/api/v1/knowledge/",
            base_url=base_url,
            api_key=api_key,
        )
        kb_list = kb_response.json()
        if "items" in kb_list and "total" in kb_list:
            kb_list = kb_list["items"]

        # Build map of file_id -> collection_name
        file_to_collection = {}
        for kb in kb_list:
            kb_name = kb.get("name", "unknown")
            for file_info in kb.get("files", []):
                file_id = file_info.get("id")
                if file_id:
                    file_to_collection[file_id] = kb_name

        response = make_request(
            method="GET", endpoint="/api/v1/files/", base_url=base_url, api_key=api_key
        )
        files = response.json()

        # Build output dict for files with non-completed status or empty content
        output = {}
        for file_info in files:
            data = file_info.get("data", {})
            status = data.get("status")
            content = data.get("content", "")

            # Include files where status is not "completed" or content is empty
            # Empty content indicates processing failure even if status is "completed"
            if (status and status != "completed") or (
                status == "completed" and not content
            ):
                filename = file_info.get("meta", {}).get(
                    "name", file_info.get("filename", "unknown")
                )

                # Copy data but exclude content field
                data_copy = data.copy()
                if "content" in data_copy:
                    del data_copy["content"]

                # Add collection name if file is in a collection
                file_id = file_info.get("id")
                if file_id and file_id in file_to_collection:
                    data_copy["collection"] = file_to_collection[file_id]

                output[filename] = data_copy

        print(json.dumps(output, indent=2))
    except Exception:
        if debug:
            logger.error("Exception occurred, entering debugger...")
            pdb.post_mortem()
        raise


@cli.command()
@click.option(
    "--base-url",
    envvar="OPENWEBUI_BASE_URL",
    default="http://localhost:3000",
    help="OpenWebUI API base URL",
)
@click.option(
    "--api-key",
    envvar="OPENWEBUI_API_KEY",
    required=True,
    help="OpenWebUI API authentication key",
)
@click.option("--kb-id", envvar="OPENWEBUI_KB_ID", help="Knowledge base ID")
@click.option(
    "--kb-name",
    envvar="OPENWEBUI_KB_NAME",
    help="Knowledge base name (alternative to --kb-id)",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug mode - drop into pdb debugger on exceptions",
)
def list_kb_files(base_url, api_key, kb_id, kb_name, debug):
    """List files in a specific knowledge base.

    Specify the knowledge base using either --kb-id or --kb-name.
    """
    try:
        # Resolve knowledge base name to ID if needed
        resolved_kb_id = resolve_kb_id(kb_id, kb_name, base_url, api_key)

        response = make_request(
            method="GET",
            endpoint=f"/api/v1/knowledge/{resolved_kb_id}",
            base_url=base_url,
            api_key=api_key,
        )
        kb_data = response.json()
        print(json.dumps(kb_data, indent=2))
    except Exception:
        if debug:
            logger.error("Exception occurred, entering debugger...")
            pdb.post_mortem()
        raise


@cli.command()
@click.option(
    "--base-url",
    envvar="OPENWEBUI_BASE_URL",
    default="http://localhost:3000",
    help="OpenWebUI API base URL",
)
@click.option(
    "--api-key",
    envvar="OPENWEBUI_API_KEY",
    required=True,
    help="OpenWebUI API authentication key",
)
@click.option(
    "--dry",
    is_flag=True,
    help="Dry run - show what would be deleted without actually deleting",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug mode - drop into pdb debugger on exceptions",
)
def prune_files(base_url, api_key, dry, debug):
    """Delete files with non-completed status or empty content from OpenWebUI.

    This command identifies and deletes files whose processing status is not
    "completed" or have empty content (indicating processing failure).
    Use --dry to preview what would be deleted without actually performing
    the deletion.
    """
    try:
        response = make_request(
            method="GET", endpoint="/api/v1/files/", base_url=base_url, api_key=api_key
        )
        files = response.json()

        # Find files with non-completed status or empty content
        to_delete = []
        for file_info in files:
            data = file_info.get("data", {})
            status = data.get("status")
            content = data.get("content", "")

            # Include files where status is not "completed" or content is empty
            # Empty content indicates processing failure even if status is "completed"
            if (status and status != "completed") or (status == "completed" and not content):
                filename = file_info.get("meta", {}).get(
                    "name", file_info.get("filename", "unknown")
                )
                file_id = file_info.get("id")
                to_delete.append((file_id, filename, status))

        if not to_delete:
            logger.info("No files with non-completed status or empty content found")
            return

        logger.info(f"Found {len(to_delete)} files with non-completed status or empty content")

        # Delete or show what would be deleted
        deleted_count = 0
        for file_id, filename, status in to_delete:
            if dry:
                logger.info(
                    f"[DRY RUN] Would delete: {filename} (status: {status}, id: {file_id})"
                )
            else:
                logger.info(f"Deleting: {filename} (status: {status}, id: {file_id})")
                try:
                    make_request(
                        method="DELETE",
                        endpoint=f"/api/v1/files/{file_id}",
                        base_url=base_url,
                        api_key=api_key,
                    )
                    deleted_count += 1
                    logger.info(f"Deleted: {filename}")
                except requests.exceptions.HTTPError as e:
                    logger.error(f"Failed to delete {filename}: {e}")

        if dry:
            logger.info(f"[DRY RUN] Would delete {len(to_delete)} files")
        else:
            logger.info(
                f"Successfully deleted {deleted_count} of {len(to_delete)} files"
            )

    except Exception:
        if debug:
            logger.error("Exception occurred, entering debugger...")
            pdb.post_mortem()
        raise


@cli.command()
@click.option(
    "--base-url",
    envvar="OPENWEBUI_BASE_URL",
    default="http://localhost:3000",
    help="OpenWebUI API base URL",
)
@click.option(
    "--api-key",
    envvar="OPENWEBUI_API_KEY",
    required=True,
    help="OpenWebUI API authentication key",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug mode - drop into pdb debugger on exceptions",
)
@click.argument("file_id")
def download_file(base_url, api_key, debug, file_id):
    """Download file content by FILE_ID and write to stdout.

    Example: owui_knowledge_sync.py download abc123 > output.txt
    """
    try:
        response = make_request(
            method="GET",
            endpoint=f"/api/v1/files/{file_id}/content",
            base_url=base_url,
            api_key=api_key,
            stream=True,
        )

        # Stream content directly to stdout
        for chunk in response.iter_content(chunk_size=8192):
            sys.stdout.buffer.write(chunk)
    except Exception:
        if debug:
            logger.error("Exception occurred, entering debugger...")
            pdb.post_mortem()
        raise


if __name__ == "__main__":
    cli()
