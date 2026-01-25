#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = [
#   "click",
#   "loguru",
#   "requests",
#   "tqdm",
#   "pymupdf",
#   "pyzotero",
#   "pydantic",
# ]
# ///
"""Sync local directory files with OpenWebUI knowledge base.

This script provides a CLI tool to synchronize files from a local directory
to an OpenWebUI knowledge base. Files are tracked using modification timestamps
to detect changes, and filenames are encoded with a directory identifier to
support multiple sync directories.

This tool was developed with assistance from aider.chat.
"""

import hashlib
import json
import mimetypes
import os
import pdb
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

import click
import fitz  # pymupdf - for PDF text extraction
import requests
from loguru import logger
from pyzotero import zotero
from tqdm import tqdm

from utils.datatypes import File, KnowledgeBase, validate_response

VERSION: str = "2.0.0"

# Configure logger to write to both console and file
# Detailed logs go to file, INFO+ goes to console
logger.remove()  # Remove default handler
logger.add(
    sys.stderr,
    level="INFO",
    format="{file}:{function}:{line} - <level>{message}</level>",
)
logger.add(
    "log.txt",
    rotation="10 MB",
    retention="10 days",
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {file}:{function}:{line} - {message}",
)


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

    # Capture and log detailed error messages before raising
    if not response.ok:
        error_detail = ""
        try:
            # Try to parse response as JSON and extract error message
            error_json = response.json()
            # Common error field names in APIs
            error_detail = error_json.get(
                "error", error_json.get("message", error_json.get("detail", ""))
            )
            # If error_detail is still empty or not a string, dump the whole JSON
            if not error_detail or not isinstance(error_detail, str):
                error_detail = json.dumps(error_json)
        except Exception:
            # If JSON parsing fails, get raw text response (truncated to 500 chars)
            error_detail = (
                response.text[:500] if response.text else "No error details available"
            )

        logger.error(f"API request failed [{response.status_code}]: {error_detail}")

    response.raise_for_status()

    return response


def upload_file(
    filepath: Path,
    relative_path: str,
    kbdir_id: str,
    base_url: str,
    api_key: str,
    timeout: int = 600,
    text_content: Optional[str] = None,
) -> Dict:
    """Upload file to OpenWebUI with encoded filename and wait for processing.

    The filename is encoded with the kbdir_id prefix to support multiple
    sync directories in the same OpenWebUI instance.

    After upload, this function polls the file status every 5 seconds to wait
    for OpenWebUI to finish parsing and computing embeddings. Processing is
    considered complete when the file's data["content"] field is non-empty.

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
    timeout : int
        Maximum time to wait for processing in seconds (default: 600)
    text_content : Optional[str]
        If provided, write this text to a temporary file and upload that instead
        of reading from filepath. This allows uploading text content directly.

    Returns
    -------
    Dict
        Upload response containing file metadata including 'id' and 'hash'

    Raises
    ------
    requests.exceptions.HTTPError
        If upload fails
    click.ClickException
        If processing fails or times out
    """
    encoded_name = encode_filename(relative_path, kbdir_id)
    logger.debug(f"Uploading {relative_path} as {encoded_name}")

    # If text_content is provided, create a temporary file with that content
    # This allows uploading text extracted from PDFs or other sources
    temp_file = None
    if text_content is not None:
        temp_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        temp_file.write(text_content)
        temp_file.close()
        filepath = Path(temp_file.name)
        content_type = "text/plain"
    else:
        # Detect MIME type based on file extension
        content_type, _ = mimetypes.guess_type(str(filepath))

    logger.debug(f"Detected content type: {content_type}")

    try:
        if content_type == "text/plain":
            file = open(filepath, "r", encoding="utf-8")
        else:
            file = open(filepath, "rb")

        if not content_type:
            files = {"file": (encoded_name, file)}
        else:
            files = {"file": (encoded_name, file, content_type)}

        response = make_request(
            method="POST",
            endpoint="/api/v1/files/",
            base_url=base_url,
            api_key=api_key,
            files=files,
        )

        out = response.json()
        file_id = out.get("id")

        if not file_id:
            raise click.ClickException(
                f"Upload failed for {relative_path}: No file ID in response"
            )

        # Wait for OpenWebUI to finish processing the file
        logger.info(f"Waiting for {relative_path} to be processed...")
        start_time = time.time()
        poll_interval = 5  # Poll every 5 seconds

        while True:
            elapsed = time.time() - start_time

            if elapsed > timeout:
                raise click.ClickException(
                    f"Timeout waiting for {relative_path} to be processed (>{timeout}s)"
                )

            # Get current file status
            file_response = make_request(
                method="GET",
                endpoint=f"/api/v1/files/{file_id}",
                base_url=base_url,
                api_key=api_key,
            )
            file_data = file_response.json()

            # Check processing status
            data = file_data.get("data", {})
            status = data.get("status")
            content = data.get("content", "")

            logger.debug(
                f"File {relative_path} status: {status}, content length: {len(content)}"
            )

            # Check if processing failed
            if status == "failed":
                error_msg = data.get("error", "Unknown error")
                raise click.ClickException(
                    f"Processing failed for {relative_path}: {error_msg}"
                )

            # Check if processing is complete (content is non-empty)
            if content:
                logger.info(
                    f"File {relative_path} processed successfully in {elapsed:.1f}s"
                )
                return out

            # Wait before next poll
            logger.debug(
                f"File {relative_path} still processing... ({elapsed:.1f}s elapsed)"
            )
            time.sleep(poll_interval)
    finally:
        # Clean up temporary file if we created one
        if temp_file is not None:
            try:
                os.unlink(temp_file.name)
            except Exception as e:
                logger.warning(f"Failed to delete temporary file: {e}")


def create_knowledge_base(
    name: str,
    description: str,
    base_url: str,
    api_key: str,
) -> str:
    """Create a new knowledge base.

    Parameters
    ----------
    name : str
        Knowledge base name
    description : str
        Knowledge base description
    base_url : str
        Base API URL
    api_key : str
        Authentication API key

    Returns
    -------
    str
        Created knowledge base ID

    Raises
    ------
    requests.exceptions.HTTPError
        If creation fails
    click.ClickException
        If response doesn't contain a knowledge base ID
    """
    logger.info(f"Creating new knowledge base: {name}")
    response = make_request(
        method="POST",
        endpoint="/api/v1/knowledge/create",
        base_url=base_url,
        api_key=api_key,
        json_data={
            "name": name,
            "description": description,
            "access_control": None,
        },
    )
    kb_data = response.json()
    kb_id = kb_data.get("id")

    if not kb_id:
        raise click.ClickException(
            f"Failed to create knowledge base '{name}': No ID in response"
        )

    logger.info(f"Created knowledge base '{name}' with ID: {kb_id}")
    return kb_id


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


def remove_file_from_kb(
    file_id: str, kb_id: str, base_url: str, api_key: str, delete_file: bool = True
) -> Dict:
    """Remove a file from a knowledge base.

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
    delete_file : bool
        If True, delete the file from storage. If False, only remove from KB.

    Returns
    -------
    Dict
        Knowledge base metadata after removing the file

    Raises
    ------
    requests.exceptions.HTTPError
        If remove operation fails
    """
    # Build endpoint with query parameter to control file deletion
    endpoint = f"/api/v1/knowledge/{kb_id}/file/remove?delete_file={'true' if delete_file else 'false'}"

    response = make_request(
        method="POST",
        endpoint=endpoint,
        base_url=base_url,
        api_key=api_key,
        json_data={"file_id": file_id},
    )
    return response.json()


def get_file_content(file_id: str, base_url: str, api_key: str) -> str:
    """Download file content from OpenWebUI by file ID.

    Parameters
    ----------
    file_id : str
        File ID to download
    base_url : str
        Base API URL
    api_key : str
        Authentication API key

    Returns
    -------
    str
        File content as text

    Raises
    ------
    requests.exceptions.HTTPError
        If download fails
    """
    response = make_request(
        method="GET",
        endpoint=f"/api/v1/files/{file_id}/content",
        base_url=base_url,
        api_key=api_key,
        stream=True,
    )

    # Collect all chunks into bytes
    content_bytes = b"".join(response.iter_content(chunk_size=8192))

    # Decode to text - try UTF-8 first, fall back to latin-1 if that fails
    try:
        cont = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # logger.warning(f"Failed to decode file {file_id} as UTF-8, trying latin-1")
        cont = content_bytes.decode("latin-1", errors="replace")

    assert cont.strip(), f"Empty file content: {file_id}"
    return cont


def compute_text_hash(text: str) -> str:
    """Compute SHA256 hash of text content for duplicate detection.

    Normalizes text by stripping leading/trailing whitespace before hashing
    to avoid false mismatches due to minor formatting differences.

    Parameters
    ----------
    text : str
        Text content to hash

    Returns
    -------
    str
        Hex-encoded SHA256 hash of the text
    """
    # Normalize by stripping whitespace - this prevents mismatches due to
    # minor formatting differences while still detecting true duplicates
    normalized = text.strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_content_hash_map(
    kb_files: List[Dict], base_url: str, api_key: str
) -> Dict[str, Dict]:
    """Build mapping of content hashes to file info for duplicate detection.

    Downloads content of all files in the knowledge base and computes their
    hashes. This allows detecting if new content is a duplicate of existing
    content, even if filenames differ.

    Parameters
    ----------
    kb_files : List[Dict]
        List of file metadata dicts from knowledge base
    base_url : str
        Base API URL
    api_key : str
        Authentication API key

    Returns
    -------
    Dict[str, Dict]
        Mapping of content_hash -> {"file_id": str, "filename": str}
        If multiple files have the same hash, only the first is kept.
    """
    hash_map = {}

    logger.info(f"Building content hash map for {len(kb_files)} files...")

    for file_info in tqdm(kb_files, desc="Hashing KB files"):
        file_id = file_info.get("id")
        filename = file_info.get("meta", {}).get("name", "unknown")

        if not file_id:
            continue

        try:
            # Download file content and compute hash
            content = get_file_content(file_id, base_url, api_key)
            content_hash = compute_text_hash(content)

            # Store first occurrence of each hash
            # If duplicates exist in KB, we keep the first one
            if content_hash not in hash_map:
                hash_map[content_hash] = {
                    "file_id": file_id,
                    "filename": filename,
                }
                logger.debug(f"Hashed {filename}: {content_hash[:16]}...")
            else:
                logger.debug(
                    f"Found duplicate content: {filename} matches {hash_map[content_hash]['filename']}"
                )

        except Exception as e:
            logger.warning(f"Failed to hash {filename}: {e}")
            continue

    logger.info(f"Built hash map with {len(hash_map)} unique content hashes")
    return hash_map


def resolve_kb_id(
    kb_id: Optional[str], kb_name: Optional[str], base_url: str, api_key: str
) -> str:
    """Resolve knowledge base name to ID, or return ID if provided.

    Exactly one of kb_id or kb_name must be provided. If kb_name is given,
    it will be looked up via the API to find the corresponding ID. If the
    name doesn't exist, a new knowledge base will be created automatically.

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
        Knowledge base ID (existing or newly created)

    Raises
    ------
    click.ClickException
        If neither kb_id nor kb_name is provided, or both are provided
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

    # No match found - create new knowledge base
    logger.info(f"Knowledge base '{kb_name}' not found, creating it...")
    return create_knowledge_base(
        name=kb_name,
        description=f"Auto-created knowledge base for {kb_name}",
        base_url=base_url,
        api_key=api_key,
    )


def build_zotero_collection_tree(
    zot: zotero.Zotero, parent_key: Optional[str] = None
) -> List[Dict]:
    """Build hierarchical tree of Zotero collections.

    Recursively constructs a tree structure of collections, starting from
    the top level (parent_key=None) or from a specific parent collection.

    Parameters
    ----------
    zot : zotero.Zotero
        Zotero API client instance
    parent_key : Optional[str]
        Parent collection key to start from. If None, starts from top-level collections.

    Returns
    -------
    List[Dict]
        List of collection tree nodes, each with 'key', 'name', and 'children' keys
    """
    all_collections = zot.collections()

    # Filter collections by parent - top-level collections have no parentCollection field
    # or it's an empty string, while child collections have a parentCollection key
    if parent_key is None:
        # Get top-level collections (no parent or empty parent)
        filtered = [
            c for c in all_collections if not c.get("data", {}).get("parentCollection")
        ]
    else:
        # Get child collections of the specified parent
        filtered = [
            c
            for c in all_collections
            if c.get("data", {}).get("parentCollection") == parent_key
        ]

    tree = []
    for collection in filtered:
        node = {
            "key": collection["key"],
            "name": collection["data"]["name"],
            "children": build_zotero_collection_tree(zot, collection["key"]),
        }
        tree.append(node)

    return tree


def find_collection_by_path(tree: List[Dict], path_parts: List[str]) -> Optional[Dict]:
    """Find collection node in tree by navigating path components.

    Traverses the collection tree following the path specified by path_parts.
    Each part represents a collection name to descend into.

    Parameters
    ----------
    tree : List[Dict]
        Collection tree structure from build_zotero_collection_tree()
    path_parts : List[str]
        List of collection names forming the path (e.g., ['A', 'B', 'C'])

    Returns
    -------
    Optional[Dict]
        The matching collection node with 'key', 'name', 'children' keys,
        or None if path not found
    """
    # If path is empty, return the entire tree (None signals to use root)
    if not path_parts:
        return None

    # Search for the first part in the current level
    target_name = path_parts[0]
    for node in tree:
        if node["name"] == target_name:
            # Found the node - if this is the last part, return it
            if len(path_parts) == 1:
                return node
            # Otherwise, recurse into children
            return find_collection_by_path(node["children"], path_parts[1:])

    # Not found
    return None


def get_items_with_paths(
    zot: zotero.Zotero,
    collection_key: str,
    all_collections: List[Dict],
    current_path: str = "",
    excluded_paths: Optional[Set[str]] = None,
) -> Dict[str, Dict]:
    """Recursively get all items in collection with their subcollection paths.

    Traverses a collection and all its subcollections, collecting items and
    their PDF attachments. Items that appear in multiple subcollections will
    have multiple paths recorded. Paths are relative to the root collection
    being synced (not including the root collection name itself).

    Parameters
    ----------
    zot : zotero.Zotero
        Zotero API client instance
    collection_key : str
        Collection key to start from
    all_collections : List[Dict]
        Complete list of all collections (fetched once at top level)
    current_path : str
        Current path prefix (used for recursion), with %% as separator
    excluded_paths : Optional[Set[str]]
        Set of collection paths to exclude from syncing (relative to sync root)

    Returns
    -------
    Dict[str, Dict]
        Dict mapping item_key to dict with:
        - 'title': item title
        - 'paths': list of collection path strings (e.g., ["SubCol1", "SubCol1%%SubCol2"])
        - 'attachments': list of attachment keys for this item
    """
    # Skip this collection if it matches an exclusion pattern
    if excluded_paths and current_path:
        for excluded in excluded_paths:
            # Check if current path is the excluded path or a child of it
            if current_path == excluded or current_path.startswith(f"{excluded}%%"):
                logger.debug(f"Skipping excluded collection path: {current_path}")
                return {}

    logger.info(
        f"Processing collection at path: '{current_path}' (key: {collection_key})"
    )

    items_dict = {}

    # Get items in this collection
    collection_items = zot.collection_items(collection_key)
    logger.info(f"Found {len(collection_items)} items directly in this collection")

    for item in collection_items:
        # Skip standalone attachments - we only want parent items with attachments
        if item.get("data", {}).get("itemType") == "attachment":
            continue

        item_key = item["key"]
        item_title = item.get("data", {}).get("title", "Untitled")

        # Get attachments for this item
        children = zot.children(item_key)
        attachment_keys = [
            child["key"]
            for child in children
            if child.get("data", {}).get("itemType") == "attachment"
        ]

        # Skip items with no attachments - nothing to sync
        if not attachment_keys:
            continue

        # Add or update item in dictionary
        if item_key not in items_dict:
            items_dict[item_key] = {
                "title": item_title,
                "paths": [],
                "attachments": attachment_keys,
            }

        # Add current path to the item's paths list (excluding empty root path)
        if current_path:
            if current_path not in items_dict[item_key]["paths"]:
                items_dict[item_key]["paths"].append(current_path)
        else:
            # Item is in root collection - mark with empty string if no paths yet
            if not items_dict[item_key]["paths"]:
                items_dict[item_key]["paths"].append("")

    logger.info(
        f"After filtering: {len(items_dict)} items with attachments in this collection"
    )

    # Recursively process subcollections
    subcollections = [
        c
        for c in all_collections
        if c.get("data", {}).get("parentCollection") == collection_key
    ]

    logger.info(f"Found {len(subcollections)} subcollections")

    for subcol in subcollections:
        subcol_name = subcol["data"]["name"]
        # Build new path: current_path%%subcol_name or just subcol_name if at root
        new_path = f"{current_path}%%{subcol_name}" if current_path else subcol_name

        # Recursively get items from subcollection
        sub_items = get_items_with_paths(
            zot, subcol["key"], all_collections, new_path, excluded_paths
        )

        # Merge subcollection items into our dict
        for sub_key, sub_data in sub_items.items():
            if sub_key not in items_dict:
                items_dict[sub_key] = sub_data
            else:
                # Item already exists - merge paths
                for path in sub_data["paths"]:
                    if path not in items_dict[sub_key]["paths"]:
                        items_dict[sub_key]["paths"].append(path)

    return items_dict


def get_attachment_text(zot: zotero.Zotero, attachment_key: str) -> str:
    """Extract text content from a Zotero attachment.

    First attempts to use Zotero's fulltext API (faster if already indexed).
    If that fails, downloads the PDF and extracts text using PyMuPDF.

    Parameters
    ----------
    zot : zotero.Zotero
        Zotero API client instance
    attachment_key : str
        Attachment item key

    Returns
    -------
    str
        Extracted text content from the attachment

    Raises
    ------
    Exception
        If text extraction fails for any reason
    """
    # Try Zotero's fulltext API first - this is faster if the file is indexed
    try:
        text_content = zot.fulltext_item(attachment_key)["content"]
        logger.debug(f"Retrieved indexed fulltext for {attachment_key}")
        return text_content
    except Exception as e:
        logger.debug(f"Fulltext not indexed for {attachment_key}, downloading PDF: {e}")

    # Fulltext not available - download PDF and extract text manually
    pdf_bytes = zot.file(attachment_key)

    # Write to temporary file and extract text with PyMuPDF
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
        tmp_file.write(pdf_bytes)
        tmp_path = tmp_file.name

    try:
        # Open PDF and extract text from all pages
        doc = fitz.open(tmp_path)
        text_content = "".join(page.get_text() for page in doc)
        doc.close()
        logger.debug(f"Extracted {len(text_content)} chars from PDF {attachment_key}")
        return text_content
    finally:
        # Clean up temporary file
        try:
            os.unlink(tmp_path)
        except Exception as e:
            logger.warning(f"Failed to delete temporary PDF file: {e}")


def generate_zotero_filename(
    title: str, paths: List[str], attachment_index: int, max_length: int = 200
) -> str:
    """Generate encoded filename for Zotero item with attachment.

    Creates a filename that encodes the item's location in the collection
    hierarchy and handles items that appear in multiple subcollections.
    Long filenames are automatically truncated to avoid filesystem limits.

    Filename format:
    - Root collection only: {title}.txt or {title}_2.txt for additional attachments
    - Single subcollection: {path}%%{title}.txt
    - Multiple subcollections: {path1}&&{path2}%%{title}.txt
    - If truncated: {path}%%{truncated_title}...{suffix}.txt

    Parameters
    ----------
    title : str
        Item title (will be sanitized)
    paths : List[str]
        List of collection paths where this item appears
    attachment_index : int
        Index of attachment (0 for first, 1 for second, etc.)
    max_length : int
        Maximum filename length to avoid filesystem limits (default: 200)

    Returns
    -------
    str
        Generated filename with .txt extension, truncated if necessary
    """
    # Sanitize title - remove HTML tags and replace path separators with underscores
    # Strip HTML tags like <span>, <em>, etc. that Zotero includes in titles
    sanitized_title = re.sub(r"<[^>]+>", "", title)
    # Replace path separators with underscores
    sanitized_title = sanitized_title.replace("/", "_").replace("\\", "_")

    # Build path prefix based on number of paths
    if not paths or (len(paths) == 1 and paths[0] == ""):
        # Item only in root collection
        path_prefix = ""
    elif len(paths) == 1:
        # Item in single subcollection
        path_prefix = f"{paths[0]}%%"
    else:
        # Item in multiple subcollections - sort paths and join with &&
        sorted_paths = sorted(paths)
        joined_paths = "&&".join(sorted_paths)
        path_prefix = f"{joined_paths}%%"

    # Add attachment index suffix if needed (for items with multiple attachments)
    if attachment_index > 0:
        suffix = f"_{attachment_index + 1}"
    else:
        suffix = ""

    # Extension is always .txt
    extension = ".txt"

    # Build initial filename
    filename = f"{path_prefix}{sanitized_title}{suffix}{extension}"

    # Check if truncation is needed
    if len(filename) > max_length:
        # Calculate space available for title
        # We need: path_prefix + title_with_marker + suffix + extension <= max_length
        # Where: title_with_marker = truncated_title + "..."
        truncation_marker = "..."
        fixed_overhead = len(path_prefix) + len(suffix) + len(extension)
        max_title_with_marker = max_length - fixed_overhead
        max_truncated_title = max_title_with_marker - len(truncation_marker)

        # Ensure minimum title length to keep filenames meaningful
        if max_truncated_title < 10:
            max_truncated_title = 10

        # Truncate title and rebuild filename
        truncated_title = sanitized_title[:max_truncated_title] + truncation_marker
        filename = f"{path_prefix}{truncated_title}{suffix}{extension}"

    return filename


def sync_zotero_collection(
    zot: zotero.Zotero,
    collection_key: str,
    root_collection_name: str,
    kb_id: str,
    kbdir_id: str,
    base_url: str,
    api_key: str,
    excluded_paths: Optional[Set[str]] = None,
    timeout: int = 1800,
    method: str = "hash",
    dry: bool = False,
    debug: bool = False,
) -> None:
    """Synchronize Zotero collection to OpenWebUI knowledge base.

    Extracts text content from PDF attachments in a Zotero collection and
    uploads them to an OpenWebUI knowledge base. Files are named to encode
    their position in the collection hierarchy, allowing items to be organized
    and supporting items that appear in multiple subcollections.

    Unlike directory sync, this is upload-only - existing files in the KB
    are not deleted. Files with matching names are skipped (assumed up-to-date).

    Parameters
    ----------
    zot : zotero.Zotero
        Zotero API client instance
    collection_key : str
        Zotero collection key to sync
    root_collection_name : str
        Name of root collection (for logging)
    kb_id : str
        Knowledge base ID
    kbdir_id : str
        Knowledge base directory identifier for file naming
    base_url : str
        Base API URL
    api_key : str
        Authentication API key
    excluded_paths : Optional[Set[str]]
        Set of collection paths to exclude from syncing (relative to sync root)
    timeout : int
        Maximum time to wait for file processing in seconds (default: 1800)
    method : str
        Duplicate detection method: "hash" (check content hashes) or "name" (filename only)
    dry : bool
        If True, show what would be done without making changes
    debug : bool
        If True, raise exceptions immediately instead of continuing

    Raises
    ------
    click.ClickException
        If sync completes with failures and not in dry run mode
    """
    if dry:
        logger.info(
            f"[DRY RUN] Starting Zotero sync preview for collection '{root_collection_name}'"
        )
    else:
        logger.info(f"Starting Zotero sync for collection '{root_collection_name}'")

    # Step 1: Fetch all collections once at the top level for efficiency
    logger.info("Fetching all collections from Zotero...")
    all_collections = zot.collections()
    logger.info(f"Retrieved {len(all_collections)} total collections from Zotero")

    # Step 2: Get all items with their paths and attachments
    logger.info("Fetching Zotero items and attachments...")
    if excluded_paths:
        logger.info(
            f"Excluding {len(excluded_paths)} collection path(s): {', '.join(sorted(excluded_paths))}"
        )
    items_dict = get_items_with_paths(
        zot, collection_key, all_collections, excluded_paths=excluded_paths
    )
    logger.info(
        f"Found {len(items_dict)} items with attachments in collection hierarchy"
    )

    # Step 3: Get existing files in knowledge base
    logger.info(f"Fetching existing files in knowledge base {kb_id}...")
    all_files_response = make_request(
        method="GET", endpoint="/api/v1/files/", base_url=base_url, api_key=api_key
    )
    all_files = all_files_response.json()

    # Build set of existing filenames for this kbdir_id and file IDs in KB
    # We track both to handle duplicate content properly:
    # - existing_files: filenames decoded for this kbdir_id (across ALL files)
    # - kb_file_ids: all file IDs in this KB (for checking if duplicates are already present)
    existing_files = set()
    kb_file_ids = set()
    kb_files = []
    for file_info in all_files:
        # Check if file is in this knowledge base
        is_in_kb = file_info.get("meta", {}).get("collection_name") == kb_id

        if is_in_kb:
            # Track file ID for duplicate detection
            file_id = file_info.get("id")
            if file_id:
                kb_file_ids.add(file_id)
            # Track file for cleanup logic
            kb_files.append(file_info)

        # Track filename for this kbdir_id (check all files, not just those in this KB)
        # This prevents re-uploading files that were uploaded but failed to be added to KB
        # Keep %% format to match generate_zotero_filename() output
        encoded_name = file_info.get("meta", {}).get("name", "")
        prefix = f"{kbdir_id}%%"
        if encoded_name.startswith(prefix):
            filename_without_prefix = encoded_name[len(prefix) :]
            existing_files.add(filename_without_prefix)

    logger.info(f"Found {len(existing_files)} existing files for kbdir_id '{kbdir_id}'")
    logger.info(f"Found {len(kb_file_ids)} total files in knowledge base {kb_id}")

    # Step 4: Build content hash map for duplicate detection (unless method is "name")
    # This prevents uploading duplicate content under different filenames
    if method == "hash":
        logger.info("Building content hash map for duplicate detection...")
        content_hash_map = build_content_hash_map(all_files, base_url, api_key)
    else:
        logger.info(
            "Using name-based matching (--method=name), skipping hash computation"
        )
        content_hash_map = {}

    # Step 4.5: Clean up files no longer in Zotero collection
    logger.info("Checking for files to remove from knowledge base...")

    # Build set of expected filenames based on Zotero items
    expected_filenames = set()
    for item_key, item_data in items_dict.items():
        title = item_data["title"]
        paths = item_data["paths"]
        attachments = item_data["attachments"]

        for idx in range(len(attachments)):
            filename = generate_zotero_filename(title, paths, idx)
            expected_filenames.add(filename)

    logger.info(f"Expecting {len(expected_filenames)} files based on Zotero collection")

    # Build map of file_id -> set of kbdir_ids that use it
    # This determines if a file is shared across sync directories
    file_id_to_kbdirs: Dict[str, Set[str]] = {}

    for file_info in all_files:
        file_id = file_info.get("id")
        if not file_id:
            continue

        encoded_name = file_info.get("meta", {}).get("name", "")
        # Try to extract kbdir_id by splitting on first %%
        if "%%" in encoded_name:
            extracted_kbdir = encoded_name.split("%%")[0]
            if file_id not in file_id_to_kbdirs:
                file_id_to_kbdirs[file_id] = set()
            file_id_to_kbdirs[file_id].add(extracted_kbdir)

    # Check each file in KB for removal
    removed_count = 0
    deleted_count = 0
    failed_files = []

    for kb_file_data in kb_files:
        # Handle both validated File objects and raw dicts
        if isinstance(kb_file_data, File):
            kb_file = kb_file_data.model_dump()
        else:
            kb_file = kb_file_data

        encoded_name = kb_file.get("meta", {}).get("name", "")
        # Extract filename without kbdir_id prefix, keeping %% format to match expected_filenames
        prefix = f"{kbdir_id}%%"
        if not encoded_name.startswith(prefix):
            # Not from our sync directory, skip
            continue

        filename_without_prefix = encoded_name[len(prefix) :]

        # Check if this file is still expected in the collection
        if filename_without_prefix in expected_filenames:
            # File is still in Zotero collection, keep it
            continue

        # File is no longer in Zotero collection - remove it
        file_id = kb_file.get("id")

        if not file_id:
            logger.warning(f"Cannot remove file without ID: {filename_without_prefix}")
            continue

        # Determine if file is used by other kbdir_ids
        kbdirs_using_file = file_id_to_kbdirs.get(file_id, set())
        is_shared = len(kbdirs_using_file) > 1 or (
            len(kbdirs_using_file) == 1 and kbdir_id not in kbdirs_using_file
        )

        if is_shared:
            # File is used by other sync directories - just remove from current KB
            if dry:
                logger.info(
                    f"[DRY RUN] Would remove from KB (shared with other dirs): {filename_without_prefix}"
                )
                removed_count += 1
            else:
                logger.info(
                    f"Removing from KB (shared with other dirs): {filename_without_prefix}"
                )
                try:
                    remove_file_from_kb(
                        file_id, kb_id, base_url, api_key, delete_file=False
                    )
                    removed_count += 1
                    logger.info(
                        f"Removed from KB (file preserved): {filename_without_prefix}"
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to remove {filename_without_prefix} from KB: {e}"
                    )
                    failed_files.append(
                        (filename_without_prefix, f"remove from KB failed - {e}")
                    )
                    if debug:
                        raise
        else:
            # File is only used by this sync directory - delete entirely
            if dry:
                logger.info(
                    f"[DRY RUN] Would delete entirely (not in other dirs): {filename_without_prefix}"
                )
                deleted_count += 1
            else:
                logger.info(
                    f"Deleting entirely (not in other dirs): {filename_without_prefix}"
                )
                try:
                    remove_file_from_kb(
                        file_id, kb_id, base_url, api_key, delete_file=True
                    )
                    deleted_count += 1
                    logger.info(
                        f"Deleted from KB and storage: {filename_without_prefix}"
                    )
                except Exception as e:
                    logger.error(f"Failed to delete {filename_without_prefix}: {e}")
                    failed_files.append(
                        (filename_without_prefix, f"delete failed - {e}")
                    )
                    if debug:
                        raise

    if dry:
        logger.info(
            f"[DRY RUN] Would remove {removed_count} files from KB, delete {deleted_count} files entirely"
        )
    else:
        logger.info(
            f"Cleanup: {removed_count} removed from KB, {deleted_count} deleted entirely"
        )

    # Step 5: Process each item and its attachments
    uploaded_count = 0
    added_count = 0
    skipped_count = 0
    skipped_duplicate_count = 0

    # Calculate total attachments for progress bar
    total_attachments = sum(len(item["attachments"]) for item in items_dict.values())

    with tqdm(
        total=total_attachments, desc="Processing attachments", disable=dry
    ) as pbar:
        for item_key, item_data in items_dict.items():
            title = item_data["title"]
            paths = item_data["paths"]
            attachments = item_data["attachments"]

            for idx, attachment_key in enumerate(attachments):
                # Generate filename for this attachment
                filename = generate_zotero_filename(title, paths, idx)

                # Check if file already exists in KB
                if filename in existing_files:
                    logger.debug(f"Skipping (already in KB): {filename}")
                    skipped_count += 1
                    pbar.update(1)
                    continue

                # File needs to be uploaded
                if dry:
                    logger.info(f"[DRY RUN] Would upload: {filename}")
                    uploaded_count += 1
                    pbar.update(1)
                    continue

                # Extract text content from attachment
                logger.info(f"Extracting text from: {title} (attachment {idx + 1})")
                try:
                    text_content = get_attachment_text(zot, attachment_key)

                    if not text_content or not text_content.strip():
                        logger.warning(f"No text extracted from {filename}, skipping")
                        failed_files.append((filename, "no text content"))
                        pbar.update(1)
                        continue

                    logger.info(
                        f"Extracted {len(text_content)} characters from {filename}"
                    )
                except Exception as e:
                    logger.error(f"Failed to extract text from {filename}: {e}")
                    failed_files.append((filename, f"text extraction failed - {e}"))
                    if debug:
                        raise
                    pbar.update(1)
                    continue

                # Check if content is a duplicate before uploading (only if method is "hash")
                if method == "hash":
                    text_hash = compute_text_hash(text_content)
                else:
                    text_hash = None

                if text_hash and text_hash in content_hash_map:
                    # Content already exists somewhere in OpenWebUI
                    existing = content_hash_map[text_hash]
                    existing_file_id = existing["file_id"]
                    existing_filename = existing["filename"]

                    # Check if this file is already in the target KB
                    if existing_file_id in kb_file_ids:
                        logger.info(
                            f"Skipping {filename}: content already in KB as {existing_filename}"
                        )
                        skipped_duplicate_count += 1
                        pbar.update(1)
                        continue

                    # File exists in OpenWebUI but not in this KB - add it to the KB
                    # This saves storage (no duplicate upload) and processing time (no re-embedding)
                    logger.info(
                        f"Found duplicate content: {filename} matches existing file {existing_filename}"
                    )
                    logger.info(
                        f"Adding existing file to knowledge base instead of uploading"
                    )

                    if dry:
                        logger.info(
                            f"[DRY RUN] Would add existing file {existing_filename} to KB"
                        )
                        added_count += 1
                        pbar.update(1)
                        continue

                    try:
                        add_result = add_file_to_kb(
                            existing_file_id, kb_id, base_url, api_key
                        )

                        if not add_result.get("id"):
                            logger.error(
                                f"Failed to add existing file {existing_filename} to KB"
                            )
                            failed_files.append(
                                (filename, "add existing file to KB failed - no KB ID")
                            )
                            pbar.update(1)
                            continue

                        logger.info(
                            f"Added existing file to KB successfully: {existing_filename}"
                        )
                        added_count += 1
                        # Add to kb_file_ids so we don't try to add it again if another
                        # Zotero item references the same content
                        kb_file_ids.add(existing_file_id)
                        pbar.update(1)
                        continue
                    except Exception as e:
                        logger.error(
                            f"Failed to add existing file {existing_filename} to KB: {e}"
                        )
                        failed_files.append(
                            (filename, f"add existing file to KB failed - {e}")
                        )
                        if debug:
                            raise
                        pbar.update(1)
                        continue

                # Upload text content as a file
                logger.info(f"Uploading: {filename}")
                try:
                    # Create a dummy path for the filename - upload_file will create temp file
                    dummy_path = Path(filename)
                    upload_result = upload_file(
                        filepath=dummy_path,
                        relative_path=filename,
                        kbdir_id=kbdir_id,
                        base_url=base_url,
                        api_key=api_key,
                        timeout=timeout,
                        text_content=text_content,
                    )

                    if not upload_result.get("id"):
                        logger.error(
                            f"Upload failed for {filename}: No file ID in response"
                        )
                        failed_files.append((filename, "upload failed - no file ID"))
                        pbar.update(1)
                        continue

                    file_id = upload_result["id"]
                    logger.info(f"Uploaded successfully: {filename} ({file_id})")
                    uploaded_count += 1
                except Exception as e:
                    logger.error(f"Upload failed for {filename}: {e}")
                    failed_files.append((filename, f"upload failed - {e}"))
                    if debug:
                        raise
                    pbar.update(1)
                    continue

                # Add file to knowledge base
                logger.info(f"Adding to knowledge base: {filename}")
                try:
                    add_result = add_file_to_kb(file_id, kb_id, base_url, api_key)

                    if not add_result.get("id"):
                        logger.error(f"Failed to add {filename} to knowledge base")
                        failed_files.append((filename, "add to KB failed - no KB ID"))
                        pbar.update(1)
                        continue

                    logger.info(f"Added to KB successfully: {filename}")
                    added_count += 1
                except requests.exceptions.HTTPError as e:
                    logger.error(f"Failed to add {filename} to knowledge base: {e}")
                    failed_files.append((filename, f"add to KB failed - {e}"))
                    if debug:
                        raise
                    pbar.update(1)
                    continue

                pbar.update(1)

    # Check for failures and raise exception if any occurred
    if failed_files and not dry:
        error_summary = "\n".join(
            f"  - {path}: {reason}" for path, reason in failed_files
        )
        raise click.ClickException(
            f"Zotero sync completed with {len(failed_files)} failures:\n{error_summary}"
        )

    if dry:
        logger.info(
            f"[DRY RUN] Summary: {uploaded_count} would be uploaded, "
            f"{added_count} would be added to KB, {skipped_count} already in KB, "
            f"{removed_count} would be removed from KB, {deleted_count} would be deleted"
        )
        logger.info("[DRY RUN] Zotero synchronization preview completed!")
    else:
        logger.info(
            f"Upload summary: {uploaded_count} uploaded, {added_count} added to KB, "
            f"{skipped_count} skipped (already in KB), "
            f"{skipped_duplicate_count} skipped (duplicate content)"
        )
        logger.info(
            f"Cleanup summary: {removed_count} removed from KB, {deleted_count} deleted entirely"
        )
        logger.info("Zotero synchronization completed successfully!")


def sync_directory(
    directory: Path,
    kb_id: str,
    kbdir_id: str,
    base_url: str,
    api_key: str,
    file_regex: Optional[str] = None,
    timeout: int = 1800,
    method: str = "hash",
    dry: bool = False,
    debug: bool = False,
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
    timeout : int
        Maximum time to wait for file processing in seconds (default: 1800)
    method : str
        Duplicate detection method: "hash" (check content hashes) or "name" (filename only)
    dry : bool
        If True, show what would be done without making changes
    debug : bool
        If True, raise exceptions immediately instead of continuing

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

    # Step 3: Get all files to build hash map and reconstruct KB files list
    # The server sometimes returns empty files list even when files exist,
    # so we reconstruct it from /api/v1/files/ endpoint
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

    # Reconstruct KB files list from all files
    # Filter files that belong to this knowledge base using collection_name
    kb_files = [
        f for f in all_files if f.get("meta", {}).get("collection_name") == kb_id
    ]
    logger.info(f"Reconstructed {len(kb_files)} files for knowledge base {kb_id}")

    # Build set of file IDs in this KB for duplicate detection
    # This allows us to check if duplicate content is already in the target KB
    kb_file_ids = set()
    for file_info in kb_files:
        file_id = file_info.get("id")
        if file_id:
            kb_file_ids.add(file_id)
    logger.info(f"Tracked {len(kb_file_ids)} file IDs in knowledge base {kb_id}")

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
    failed_files = []  # Track files that failed to delete, upload, or add

    for kb_file_data in tqdm(kb_files, desc="Checking files for deletion", disable=dry):
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
                try:
                    remove_file_from_kb(kb_file["id"], kb_id, base_url, api_key)
                except Exception as e:
                    logger.error(f"Failed to delete {decoded_name}: {e}")
                    failed_files.append((decoded_name, f"delete failed - {e}"))
                    if debug:
                        raise
                    continue
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
                try:
                    remove_file_from_kb(kb_file["id"], kb_id, base_url, api_key)
                except Exception as e:
                    logger.error(f"Failed to delete {decoded_name}: {e}")
                    failed_files.append((decoded_name, f"delete failed - {e}"))
                    if debug:
                        raise
                    continue
            deleted_count += 1
        else:
            # File unchanged or remote is newer
            logger.debug(f"Keeping (up to date): {decoded_name}")

    if dry:
        logger.info(f"[DRY RUN] Would delete {deleted_count} files from knowledge base")
    else:
        logger.info(f"Deleted {deleted_count} files from knowledge base")

    # Step 5: Build content hash map for duplicate detection (unless method is "name")
    # This prevents uploading duplicate content under different filenames
    # Build from ALL files in OpenWebUI, not just KB files, to detect duplicates
    # across the entire system
    if method == "hash":
        logger.info("Building content hash map for duplicate detection...")
        content_hash_map = build_content_hash_map(all_files, base_url, api_key)
    else:
        logger.info(
            "Using name-based matching (--method=name), skipping hash computation"
        )
        content_hash_map = {}

    # Step 6: Upload and add new or changed files
    logger.info("Checking for files to add or update...")
    uploaded_count = 0
    added_count = 0
    skipped_duplicate_count = 0

    # Sort files by size (smallest first) for faster initial feedback
    # Map each file to its size and sort
    files_with_sizes = []
    for rel_path in local_files:
        abs_path = directory / rel_path
        file_size = abs_path.stat().st_size
        files_with_sizes.append((rel_path, file_size))

    # Sort by size (ascending - smallest files first)
    files_with_sizes.sort(key=lambda x: x[1])
    sorted_local_files = [rel_path for rel_path, _ in files_with_sizes]

    logger.debug(f"Sorted {len(sorted_local_files)} files by size for upload")

    for rel_path in tqdm(
        sorted_local_files, desc="Uploading and adding files", disable=dry
    ):
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

        # Check if content is a duplicate before uploading (only if method is "hash")
        if not dry and method == "hash":
            abs_path = directory / rel_path
            with open(abs_path, "r" if abs_path.suffix == ".txt" else "rb") as f:
                if abs_path.suffix == ".txt":
                    local_content = f.read()
                else:
                    # For non-text files, read as text for hashing
                    try:
                        local_content = f.read().decode("utf-8")
                    except UnicodeDecodeError:
                        local_content = f.read().decode("latin-1", errors="replace")

            local_hash = compute_text_hash(local_content)

            if local_hash in content_hash_map:
                # Content already exists somewhere in OpenWebUI
                existing = content_hash_map[local_hash]
                existing_file_id = existing["file_id"]
                existing_filename = existing["filename"]

                # Check if this file is already in the target KB
                if existing_file_id in kb_file_ids:
                    logger.info(
                        f"Skipping {rel_path}: content already in KB as {existing_filename}"
                    )
                    skipped_duplicate_count += 1
                    continue

                # File exists in OpenWebUI but not in this KB - add it to the KB
                # This saves storage (no duplicate upload) and processing time (no re-embedding)
                logger.info(
                    f"Found duplicate content: {rel_path} matches existing file {existing_filename}"
                )
                logger.info(
                    f"Adding existing file to knowledge base instead of uploading"
                )

                try:
                    add_result = add_file_to_kb(
                        existing_file_id, kb_id, base_url, api_key
                    )

                    if not add_result.get("id"):
                        logger.error(
                            f"Failed to add existing file {existing_filename} to KB"
                        )
                        failed_files.append(
                            (rel_path, "add existing file to KB failed - no KB ID")
                        )
                        continue

                    logger.info(
                        f"Added existing file to KB successfully: {existing_filename}"
                    )
                    added_count += 1
                    # Add to kb_file_ids so we don't try to add it again for another local file
                    kb_file_ids.add(existing_file_id)
                    continue
                except Exception as e:
                    logger.error(
                        f"Failed to add existing file {existing_filename} to KB: {e}"
                    )
                    failed_files.append(
                        (rel_path, f"add existing file to KB failed - {e}")
                    )
                    if debug:
                        raise
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
            try:
                upload_result = upload_file(
                    abs_path, rel_path, kbdir_id, base_url, api_key, timeout=timeout
                )

                if not upload_result.get("id"):
                    logger.error(
                        f"Upload failed for {rel_path}: No file ID in response"
                    )
                    logger.error(f"Response: {json.dumps(upload_result, indent=2)}")
                    failed_files.append((rel_path, "upload failed - no file ID"))
                    continue

                file_id = upload_result["id"]
                logger.info(f"Uploaded successfully: {rel_path} ({file_id})")
                uploaded_count += 1
            except Exception as e:
                logger.error(f"Upload failed for {rel_path}: {e}")
                failed_files.append((rel_path, f"upload failed - {e}"))
                if debug:
                    raise
                continue

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
                if debug:
                    raise
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
            f"Upload summary: {uploaded_count} uploaded, {added_count} added to KB, "
            f"{skipped_duplicate_count} skipped (duplicate content)"
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
    "--timeout",
    envvar="OPENWEBUI_TIMEOUT",
    default=1800,
    type=int,
    help="Maximum time to wait for file processing in seconds (default: 1800 = 30 minutes)",
)
@click.option(
    "--method",
    type=click.Choice(["hash", "name"], case_sensitive=False),
    default="name",
    help="Duplicate detection method: 'hash' checks content hashes (slower but accurate), 'name' only checks filenames (faster but may miss duplicates)",
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
    base_url,
    api_key,
    kb_id,
    kb_name,
    kbdir_id,
    file_regex,
    timeout,
    method,
    dry,
    debug,
    directory,
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
            timeout=timeout,
            method=method,
            dry=dry,
            debug=debug,
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
    "--zotero-library-id",
    envvar="ZOTERO_LIBRARY_ID",
    required=True,
    help="Zotero library ID",
)
@click.option(
    "--zotero-library-type",
    envvar="ZOTERO_LIBRARY_TYPE",
    default="user",
    help="Zotero library type (user or group)",
)
@click.option(
    "--zotero-api-key",
    envvar="ZOTERO_API_KEY",
    required=True,
    help="Zotero API key",
)
@click.option(
    "--zotero-hierarchy",
    required=True,
    help="Collection hierarchy path (e.g., 'A%%B%%C' or 'TopLevel' for root)",
)
@click.option(
    "--zotero-exclude",
    multiple=True,
    help="Collection hierarchy paths to exclude (e.g., 'A%%B%%C'). Can be specified multiple times.",
)
@click.option(
    "--kb-id",
    envvar="OPENWEBUI_KB_ID",
    help="Knowledge base ID",
)
@click.option(
    "--kb-name",
    envvar="OPENWEBUI_KB_NAME",
    help="Knowledge base name (alternative to --kb-id)",
)
@click.option(
    "--kbdir-id",
    help="Unique identifier for this sync (defaults to collection name)",
)
@click.option(
    "--timeout",
    envvar="OPENWEBUI_TIMEOUT",
    default=1800,
    type=int,
    help="Maximum time to wait for file processing in seconds (default: 1800 = 30 minutes)",
)
@click.option(
    "--method",
    type=click.Choice(["hash", "name"], case_sensitive=False),
    default="name",
    help="Duplicate detection method: 'hash' checks content hashes (slower but accurate), 'name' only checks filenames (faster but may miss duplicates)",
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
def sync_zotero(
    base_url,
    api_key,
    zotero_library_id,
    zotero_library_type,
    zotero_api_key,
    zotero_hierarchy,
    zotero_exclude,
    kb_id,
    kb_name,
    kbdir_id,
    timeout,
    method,
    dry,
    debug,
):
    """Sync Zotero collection to OpenWebUI knowledge base.

    Extracts text content from PDF attachments in the specified Zotero collection
    and uploads them to an OpenWebUI knowledge base. Files are named to preserve
    the collection hierarchy structure.

    Zotero API credentials (--zotero-library-id, --zotero-library-type, and
    --zotero-api-key) can be provided as command-line options or via environment
    variables ZOTERO_LIBRARY_ID, ZOTERO_LIBRARY_TYPE, and ZOTERO_API_KEY.

    The --zotero-hierarchy parameter specifies the path to the collection using
    %% as the separator (e.g., 'ParentCollection%%SubCollection'). To sync a
    top-level collection, just provide its name.

    The --zotero-exclude parameter allows excluding specific subcollections from
    syncing. Specify the full hierarchy path (e.g., 'A%%B%%C' to exclude subcollection
    C when syncing A%%B). Can be specified multiple times to exclude multiple paths.

    Specify the knowledge base using either --kb-id or --kb-name.
    """
    try:
        # Initialize Zotero client
        logger.info(
            f"Connecting to Zotero library {zotero_library_id} ({zotero_library_type})"
        )
        zot = zotero.Zotero(zotero_library_id, zotero_library_type, zotero_api_key)

        # Parse hierarchy path
        path_parts = zotero_hierarchy.split("%%")
        logger.info(f"Looking for collection path: {' > '.join(path_parts)}")

        # Build collection tree
        logger.info("Building collection tree...")
        tree = build_zotero_collection_tree(zot)

        # Find target collection
        target_node = find_collection_by_path(tree, path_parts)

        if target_node is None:
            # Collection not found - build helpful error message
            available = []

            def collect_names(nodes, prefix=""):
                for node in nodes:
                    path = f"{prefix}{node['name']}"
                    available.append(path)
                    if node["children"]:
                        collect_names(node["children"], f"{path}%%")

            collect_names(tree)

            raise click.ClickException(
                f"Collection '{zotero_hierarchy}' not found.\n"
                f"Available collections:\n  " + "\n  ".join(sorted(available))
            )

        collection_key = target_node["key"]
        collection_name = target_node["name"]
        logger.info(f"Found collection '{collection_name}' (key: {collection_key})")

        # Process exclusion list - convert from absolute to relative paths
        excluded_paths = None
        if zotero_exclude:
            excluded_paths = set()
            hierarchy_prefix = zotero_hierarchy + "%%"

            for exclude_path in zotero_exclude:
                # Convert absolute exclusion path to relative path
                # e.g., if hierarchy is "A%%B" and exclusion is "A%%B%%C", relative path is "C"
                if exclude_path == zotero_hierarchy:
                    logger.warning(
                        f"Cannot exclude the root collection itself: {exclude_path}"
                    )
                    continue
                elif exclude_path.startswith(hierarchy_prefix):
                    relative_path = exclude_path[len(hierarchy_prefix) :]
                    excluded_paths.add(relative_path)
                    logger.info(f"Will exclude subcollection: {relative_path}")
                else:
                    logger.warning(
                        f"Exclusion path '{exclude_path}' is not a subcollection of '{zotero_hierarchy}', ignoring"
                    )

        # Resolve KB ID
        resolved_kb_id = resolve_kb_id(kb_id, kb_name, base_url, api_key)

        # Use kbdir_id if provided, otherwise use collection name
        resolved_kbdir_id = kbdir_id if kbdir_id else collection_name

        # Sync the collection
        sync_zotero_collection(
            zot=zot,
            collection_key=collection_key,
            root_collection_name=collection_name,
            kb_id=resolved_kb_id,
            kbdir_id=resolved_kbdir_id,
            base_url=base_url,
            api_key=api_key,
            excluded_paths=excluded_paths,
            timeout=timeout,
            method=method,
            dry=dry,
            debug=debug,
        )
    except Exception:
        if debug:
            logger.exception("Exception occurred during Zotero sync:")
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
    "--full",
    is_flag=True,
    help="Return full file data instead of truncated content",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug mode - drop into pdb debugger on exceptions",
)
def list_kb_files(base_url, api_key, kb_id, kb_name, full, debug):
    """List files in a specific knowledge base.

    Specify the knowledge base using either --kb-id or --kb-name.
    """
    try:
        # Resolve knowledge base name to ID if needed
        resolved_kb_id = resolve_kb_id(kb_id, kb_name, base_url, api_key)

        # Get knowledge base metadata
        response = make_request(
            method="GET",
            endpoint=f"/api/v1/knowledge/{resolved_kb_id}",
            base_url=base_url,
            api_key=api_key,
        )
        kb_data = response.json()

        # Reconstruct files list from /api/v1/files/ endpoint
        # The server sometimes returns empty files list even when files exist
        logger.info("Fetching all files to reconstruct knowledge base files list...")
        all_files_response = make_request(
            method="GET", endpoint="/api/v1/files/", base_url=base_url, api_key=api_key
        )
        all_files = all_files_response.json()

        # Filter files that belong to this knowledge base
        # file["meta"]["collection_name"] contains the KB ID (despite the name)
        kb_files = [
            f
            for f in all_files
            if f.get("meta", {}).get("collection_name") == resolved_kb_id
        ]

        logger.info(
            f"Reconstructed {len(kb_files)} files for knowledge base {resolved_kb_id}"
        )

        # Truncate file content by default unless --full is specified
        if not full:
            for file_info in kb_files:
                if "data" in file_info:
                    if "content" in file_info["data"]:
                        content = file_info["data"]["content"]
                        if isinstance(content, str) and len(content) > 100:
                            file_info["data"]["content"] = content[:100] + "[TRUNCATED]"
                    else:
                        data_str = str(file_info["data"])
                        if len(data_str) > 100:
                            file_info["data"] = data_str[:100] + "[TRUNCATED]"

        # Replace the files field with our reconstructed list
        kb_data["files"] = kb_files

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
            if (status and status != "completed") or (
                status == "completed" and not content
            ):
                filename = file_info.get("meta", {}).get(
                    "name", file_info.get("filename", "unknown")
                )
                file_id = file_info.get("id")
                to_delete.append((file_id, filename, status))

        if not to_delete:
            logger.info("No files with non-completed status or empty content found")
            return

        logger.info(
            f"Found {len(to_delete)} files with non-completed status or empty content"
        )

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
