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
  - Hash-based: compares content hashes to detect true duplicates
  - Automatic file reuse: if duplicate content already exists in OpenWebUI, adds existing file to KB instead of re-uploading
  - Saves storage space and processing time
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
- `--method` - Duplicate detection: 'hash' (content-based, slower) or 'name' (filename-based, faster)
- `--dry` - Preview changes without applying them
- `--debug` - Enable debug mode with pdb debugger on exceptions

## Usage

**Recommended**: Use `uv run` to execute the script - it will automatically handle all dependencies via the PEP 723 inline header.

```bash
# Sync a directory with name-based duplicate detection (default)
uv run openwebui_knowledge_zotero_sync.py sync \
  --kb-name "My Knowledge" \
  --kbdir-id mydir \
  /path/to/dir

# Sync with hash-based duplicate detection (slower but more accurate)
uv run openwebui_knowledge_zotero_sync.py sync \
  --kb-name "My Knowledge" \
  --kbdir-id mydir \
  --method hash \
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

# Sync Zotero collection with exclusions
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

# Check file processing status
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

### Directory Sync
1. **Delete Phase**: Removes files from KB that:
   - No longer exist locally
   - Have been modified locally (local mtime > remote updated_at)
2. **Upload Phase**: 
   - Checks for duplicate content using hash comparison (if `--method hash`)
   - Reuses existing files if content already exists in OpenWebUI
   - Uploads new/changed files and adds them to the KB
   - Waits for OpenWebUI to finish processing each file before continuing

### Zotero Sync
1. **Cleanup Phase**: Removes files from KB that are no longer in the Zotero collection
   - If file is shared with other sync directories: removes from current KB only
   - If file is unique to this sync: deletes from KB and storage
2. **Upload Phase**:
   - Extracts text from PDF attachments
   - Checks for duplicate content (if `--method hash`)
   - Reuses existing files if content already exists
   - Uploads new content with hierarchy-preserving filenames
   - Waits for processing to complete

### Duplicate Detection Methods
- **`--method name`** (default): Only checks filenames
  - Faster but may upload duplicate content under different names
  - Useful when you're confident filenames are unique or for automated syncs
- **`--method hash`**: Downloads all file content and compares SHA256 hashes
  - Slower but prevents duplicate content even if filenames differ
  - Automatically reuses existing files when duplicate content is found
  - Saves storage space and processing time

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
