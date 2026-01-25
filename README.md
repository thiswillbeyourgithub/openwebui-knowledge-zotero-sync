# OpenWebUI Knowledge Zotero Sync

Fork from https://github.com/stoerr/openwebui-knowledgesync

**⚠️ Work in Progress**

This tool is under active development. There are many situations and file types that don't work properly yet. Contributions are welcome! Please feel free to open issues or submit pull requests.

## What it can do

- **Sync local directories** to OpenWebUI knowledge bases
  - Timestamp-based change detection (compares local mtime with remote updated_at)
  - Automatic cleanup: removes files from KB that no longer exist locally or are outdated
  - Two-phase sync: delete outdated files first, then upload new/changed files
- **Sync Zotero collections** by extracting text from PDF attachments
  - Preserves collection hierarchy in filenames
  - Handles items in multiple subcollections
  - Automatic cleanup: removes files no longer in Zotero collection
  - Smart deletion: files shared across sync directories are removed from KB only, not deleted
  - **Note**: This tool is read-only with respect to Zotero - it will never modify, delete, or add anything to your Zotero collections or libraries
- **Intelligent duplicate detection**
  - Name-based: faster but only checks filenames (default)
  - Hash-based: compares content hashes to detect true duplicates across all files
  - Automatic file reuse: if duplicate content already exists in OpenWebUI, adds existing file to KB instead of re-uploading
  - Saves storage space and processing time
  - Force-duplicate mode: retries with modified content if duplicate errors occur
- **List knowledge bases** with simplified or full output
- **List all files** in OpenWebUI with optional content truncation
- **Check file processing status** to find failed uploads
- **List files in specific knowledge bases** with reconstructed file lists
- **Prune failed files** to clean up incomplete uploads
- **Download file content** by ID to stdout
- **Support multiple sync directories** per knowledge base via unique identifiers
- **Filter files** using regular expressions during sync
- **Exclude subcollections** from Zotero sync via pattern matching
- **Dry-run mode** for previewing changes before applying them

## Configuration

Configure via command-line options or environment variables:

**OpenWebUI Settings** (`OPENWEBUI_*` prefix):
- `OPENWEBUI_BASE_URL` - API base URL (default: http://localhost:3000)
- `OPENWEBUI_API_KEY` - Authentication key (required)
- `OPENWEBUI_KB_ID` or `OPENWEBUI_KB_NAME` - Knowledge base to sync with
- `OPENWEBUI_KBDIR_ID` - Unique identifier for sync directory
- `OPENWEBUI_TIMEOUT` - Max seconds to wait for file processing (default: 1800)

**Zotero Settings** (`ZOTERO_*` prefix):
- `ZOTERO_LIBRARY_ID` - Zotero library ID (required for Zotero sync)
- `ZOTERO_LIBRARY_TYPE` - Library type: 'user' or 'group' (default: user)
- `ZOTERO_API_KEY` - Zotero API authentication key (required for Zotero sync)

**Sync Options**:
- `--method` - Duplicate detection: 'hash' (content-based, slower) or 'name' (filename-based, faster, default)
- `--force-duplicate` - If duplicate content detected, modify it slightly (append MD5 hash) and retry up to 10 times
- `--dry` - Preview changes without applying them
- `--debug` - Enable debug mode with pdb debugger on exceptions
- `--file-regex` - Filter files using regular expressions (directory sync only)

## Usage

**Recommended**: Use `uv run` to execute the script - it will automatically handle all dependencies via the PEP 723 inline header.

```bash
# Sync a directory with name-based duplicate detection (default, faster)
# This only checks filenames - files with different names won't be detected as duplicates
uv run openwebui_knowledge_zotero_sync.py sync \
  --kb-name "My Knowledge" \
  --kbdir-id mydir \
  /path/to/dir

# Sync with hash-based duplicate detection (slower but detects all duplicates)
# Downloads all file content to compute hashes - prevents duplicate content even with different filenames
uv run openwebui_knowledge_zotero_sync.py sync \
  --kb-name "My Knowledge" \
  --kbdir-id mydir \
  --method hash \
  /path/to/dir

# Force upload even if duplicate content is detected
# Modifies content slightly by appending MD5 hash, retries up to 10 times
uv run openwebui_knowledge_zotero_sync.py sync \
  --kb-name "My Knowledge" \
  --kbdir-id mydir \
  --force-duplicate \
  /path/to/dir

# Sync only markdown files using regex filter
uv run openwebui_knowledge_zotero_sync.py sync \
  --kb-name "My Knowledge" \
  --kbdir-id mydir \
  --file-regex '.*\.md$' \
  /path/to/dir

# Preview what would be synced without making changes
uv run openwebui_knowledge_zotero_sync.py sync \
  --kb-name "My Knowledge" \
  --kbdir-id mydir \
  --dry \
  /path/to/dir

# Sync a Zotero collection (extracts text from PDFs)
uv run openwebui_knowledge_zotero_sync.py sync-zotero \
  --zotero-library-id 123456 \
  --zotero-api-key YOUR_KEY \
  --zotero-hierarchy "Research%%Machine Learning" \
  --kb-name "ML Papers"

# Sync Zotero collection with exclusions (provide full paths)
# Excludes "Archive" and "Drafts" subcollections from the "Research" collection
uv run openwebui_knowledge_zotero_sync.py sync-zotero \
  --zotero-hierarchy "Research" \
  --zotero-exclude "Research%%Archive" \
  --zotero-exclude "Research%%Drafts" \
  --kb-name "Active Research"

# Sync Zotero with dry-run to preview changes
uv run openwebui_knowledge_zotero_sync.py sync-zotero \
  --zotero-hierarchy "Research" \
  --kb-name "ML Papers" \
  --dry

# List knowledge bases
uv run openwebui_knowledge_zotero_sync.py list-kb

# List files in a specific knowledge base
uv run openwebui_knowledge_zotero_sync.py list-kb-files --kb-name "My Knowledge"

# Check file processing status (shows files with non-completed status or empty content)
uv run openwebui_knowledge_zotero_sync.py files-status

# Clean up failed uploads (preview first)
uv run openwebui_knowledge_zotero_sync.py prune-files --dry

# Actually delete failed files
uv run openwebui_knowledge_zotero_sync.py prune-files

# Download a specific file's content
uv run openwebui_knowledge_zotero_sync.py download-file abc123 > output.txt
```

Run any command with `--help` for more details.

## How Sync Works

### File Naming Convention

Files are encoded with a unique identifier prefix to support multiple sync directories:
- **Format**: `kbdir-id%%path%%to%%file.txt`
- **Example**: `mydir%%docs%%readme.txt` for file `docs/readme.txt` in sync directory with ID `mydir`
- **Purpose**: Allows multiple directories to sync to the same knowledge base without filename conflicts

For Zotero sync:
- **Format**: `kbdir-id%%subcol1%%subcol2%%item_title.txt` or `kbdir-id%%subcol1&&subcol2%%item_title.txt` (items in multiple collections)
- **Example**: `papers%%ML%%transformers%%Attention_Is_All_You_Need.txt`

### Directory Sync
1. **Delete Phase**: Removes files from KB that:
   - No longer exist locally
   - Have been modified locally (local mtime > remote updated_at)
2. **Upload Phase**: 
   - Sorts files by size (smallest first) for faster initial feedback
   - Checks for duplicate content using hash comparison (if `--method hash`)
   - Reuses existing files if content already exists in OpenWebUI (saves storage and processing time)
   - Uploads new/changed files and adds them to the KB
   - Polls file status every 5 seconds until processing completes (content field becomes non-empty)

### Zotero Sync
1. **Cleanup Phase**: Removes files from KB that are no longer in the Zotero collection
   - If file is shared with other sync directories: removes from current KB only  
   - If file is unique to this sync: deletes from KB and storage
2. **Upload Phase**:
   - Recursively processes collection and all subcollections (unless excluded)
   - Attempts to use Zotero's fulltext API first (faster if indexed)
   - Falls back to downloading PDF and extracting with PyMuPDF if needed
   - Generates filenames encoding collection hierarchy: `kbdir%%subcol%%title.txt`
   - Items in multiple subcollections: `kbdir%%subcol1&&subcol2%%title.txt`
   - Checks for duplicate content (if `--method hash`)
   - Reuses existing files if content already exists
   - Uploads new content with hierarchy-preserving filenames
   - Polls file status every 5 seconds until processing completes

### Duplicate Detection Methods
- **`--method name`** (default): Only checks encoded filenames
  - Faster - no content download needed
  - May upload duplicate content if files have different names
  - Best for: automated syncs where you control filenames, or when speed matters more than storage
  - Still reuses files if same filename exists (via kbdir-id prefix)
  
- **`--method hash`**: Downloads all file content and compares SHA256 hashes  
  - Slower - downloads every file to compute hash
  - Prevents duplicate content even if filenames differ
  - Automatically reuses existing files when duplicate content is found
  - Best for: initial syncs, one-time migrations, or when minimizing storage is critical
  - Saves storage space and processing time by avoiding redundant uploads and embeddings

### Force Duplicate Mode

The `--force-duplicate` flag handles cases where OpenWebUI rejects uploads due to duplicate content:
- Appends MD5 hash of content as a comment: `\n\n<!-- MD5: abc123... -->`
- Retries upload up to 10 times with different hashes
- Useful when you need to force multiple copies of the same content
- Works with both directory and Zotero sync

## Multiple Sync Directories

The `kbdir-id` parameter allows multiple directories to sync to the same knowledge base:

```bash
# Sync directory A
uv run openwebui_knowledge_zotero_sync.py sync \
  --kb-name "Shared KB" \
  --kbdir-id dirA \
  /path/to/dirA

# Sync directory B to same KB
uv run openwebui_knowledge_zotero_sync.py sync \
  --kb-name "Shared KB" \
  --kbdir-id dirB \
  /path/to/dirB
```

Each file is prefixed with its `kbdir-id` (e.g., `dirA%%file.txt` vs `dirB%%file.txt`), preventing conflicts. When you sync directory A, only files with the `dirA%%` prefix are managed - files from directory B are left untouched.

**Benefits**:
- Share embeddings across multiple sources in one knowledge base
- Organize content by source while querying everything together
- Independent sync schedules for different directories

## Troubleshooting

### Zotero "text extraction failed" Errors

If Zotero sync fails with "text extraction failed" errors, it means some PDF files have not been indexed by Zotero. To fix this:

1. Open Zotero's Settings (Preferences)
2. Go to the **Advanced** tab
3. Click **"Rebuild Index..."** button
4. Wait for the indexing process to complete
5. Re-run the sync command

This rebuilds Zotero's fulltext index, making Zotero aware of the text content in your PDF files. Without this index, the tool cannot extract text from the attachments.

## Automated Sync with systemd

Template systemd service and timer files are provided in the `systemd/` directory for automated daily syncs:

1. **Configure the service**: Edit `systemd/openwebui-sync.service`
   - Set `User` and `WorkingDirectory`
   - Choose and configure either directory or Zotero sync command
   - Create `/etc/openwebui-sync/credentials.env` with API keys (chmod 0600)
   - Consider using `--method name` for faster automated syncs if duplicate detection isn't critical

2. **Adjust the schedule**: Edit `systemd/openwebui-sync.timer`
   - Default: daily at 2:00 AM with 10-minute random delay
   - Modify `OnCalendar` for different schedules

3. **Install and enable**:
   ```bash
   sudo cp systemd/openwebui-sync.* /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now openwebui-sync.timer
   sudo systemctl status openwebui-sync.timer
   ```

4. **Monitor**: View logs with `journalctl -u openwebui-sync.service`

---

*This tool was developed with assistance from [aider.chat](https://github.com/Aider-AI/aider/).*
