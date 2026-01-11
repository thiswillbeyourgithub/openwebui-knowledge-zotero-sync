# OpenWebUI Knowledge Sync

Fork from https://github.com/stoerr/openwebui-knowledgesync

**⚠️ Work in Progress**

## What it can do

- **Sync local directories** to OpenWebUI knowledge bases with timestamp-based change detection
- **List knowledge bases** with simplified or full output
- **List all files** in OpenWebUI with optional content truncation
- **Check file processing status** to find failed uploads
- **List files in specific knowledge bases** with reconstructed file lists
- **Prune failed files** to clean up incomplete uploads
- **Download file content** by ID to stdout
- **Support multiple sync directories** per knowledge base via unique identifiers
- **Filter files** using regular expressions during sync
- **Dry-run mode** for previewing changes before applying them

## Configuration

Configure via command-line options or `OPENWEBUI_*` environment variables:
- `OPENWEBUI_BASE_URL` - API base URL (default: http://localhost:3000)
- `OPENWEBUI_API_KEY` - Authentication key (required)
- `OPENWEBUI_KB_ID` or `OPENWEBUI_KB_NAME` - Knowledge base to sync with
- `OPENWEBUI_KBDIR_ID` - Unique identifier for sync directory

## Usage

```bash
# Sync a directory to a knowledge base
owui_knowledge_sync.py sync --kb-name "My Knowledge" --kbdir-id mydir /path/to/dir

# List knowledge bases
owui_knowledge_sync.py list-kb

# Check file processing status
owui_knowledge_sync.py files-status

# Clean up failed uploads
owui_knowledge_sync.py prune-files --dry
```

Run any command with `--help` for more details.

---

*This tool was developed with assistance from [aider.chat](https://github.com/Aider-AI/aider/).*
