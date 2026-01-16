# OpenWebUI Knowledge Sync

Fork from https://github.com/stoerr/openwebui-knowledgesync

**⚠️ Work in Progress**

This tool is under active development. There are many situations and file types that don't work properly yet. Contributions are welcome! Please feel free to open issues or submit pull requests.

## What it can do

- **Sync local directories** to OpenWebUI knowledge bases with timestamp-based change detection
- **Sync Zotero collections** by extracting text from PDF attachments and preserving collection hierarchy
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

**Zotero Settings** (`ZOTERO_*` prefix):
- `ZOTERO_LIBRARY_ID` - Zotero library ID (required for Zotero sync)
- `ZOTERO_LIBRARY_TYPE` - Library type: 'user' or 'group' (default: user)
- `ZOTERO_API_KEY` - Zotero API authentication key (required for Zotero sync)

## Usage

**Recommended**: Use `uv run` to execute the script - it will automatically handle all dependencies via the PEP 723 inline header.

```bash
# Sync a directory to a knowledge base
uv run openwebui_knowledge_zotero_sync.py sync --kb-name "My Knowledge" --kbdir-id mydir /path/to/dir

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

# List knowledge bases
uv run openwebui_knowledge_zotero_sync.py list-kb

# Check file processing status
uv run openwebui_knowledge_zotero_sync.py files-status

# Clean up failed uploads
uv run openwebui_knowledge_zotero_sync.py prune-files --dry
```

Run any command with `--help` for more details.

---

*This tool was developed with assistance from [aider.chat](https://github.com/Aider-AI/aider/).*
