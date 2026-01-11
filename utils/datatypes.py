"""Data type definitions for OpenWebUI API responses.

This module defines Pydantic models that represent the structure of API responses
from the OpenWebUI API. These models are used to validate responses and provide
type hints, making the code more robust and maintainable.

Validation failures log warnings instead of raising exceptions, allowing the
application to continue with partial data when possible.

This tool was developed with assistance from aider.chat.
"""

from typing import Any, Dict, List, Optional

from loguru import logger
from pydantic import BaseModel, Field, ValidationError


class AccessControl(BaseModel):
    """Access control configuration for a knowledge base.

    Parameters
    ----------
    read : Dict[str, List[str]]
        Read access permissions with group_ids and user_ids lists
    write : Dict[str, List[str]]
        Write access permissions with group_ids and user_ids lists
    """

    read: Dict[str, List[str]] = Field(
        default_factory=lambda: {"group_ids": [], "user_ids": []}
    )
    write: Dict[str, List[str]] = Field(
        default_factory=lambda: {"group_ids": [], "user_ids": []}
    )


class User(BaseModel):
    """User information.

    Parameters
    ----------
    id : str
        User identifier
    email : str
        User email address
    name : str
        User name
    role : str
        User role (e.g., 'admin', 'user')
    """

    id: str
    email: str
    name: str
    role: str


class FileData(BaseModel):
    """File processing data.

    Parameters
    ----------
    status : Optional[str]
        Processing status (e.g., 'success', 'failed')
    content : Optional[str]
        File content or error message
    error : Optional[str]
        Error message if processing failed
    """

    status: Optional[str] = None
    content: Optional[str] = None
    error: Optional[str] = None


class FileMeta(BaseModel):
    """File metadata.

    Parameters
    ----------
    name : str
        Original filename
    content_type : Optional[str]
        MIME type of the file
    size : int
        File size in bytes
    data : Optional[Dict[str, Any]]
        Additional metadata (e.g., knowledge_id)
    collection_name : Optional[str]
        Name of the collection if present. WARNING this is actually the
        id of the collection, not its name.
    """

    name: str
    content_type: Optional[str] = None
    size: int
    data: Optional[Dict[str, Any]] = None
    collection_name: Optional[str] = None


class File(BaseModel):
    """OpenWebUI file object.

    Parameters
    ----------
    id : str
        File identifier
    user_id : str
        Owner user identifier
    hash : str
        SHA256 hash of file content
    filename : str
        Original filename
    data : Optional[FileData]
        File processing data
    meta : FileMeta
        File metadata
    created_at : int
        Unix timestamp of creation
    updated_at : int
        Unix timestamp of last update
    """

    id: str
    user_id: str
    hash: Optional[str] = None
    filename: str
    data: Optional[FileData] = None
    meta: FileMeta
    created_at: int
    updated_at: int


class KnowledgeBase(BaseModel):
    """OpenWebUI knowledge base object.

    Parameters
    ----------
    id : str
        Knowledge base identifier
    name : str
        Knowledge base name
    description : str
        Knowledge base description
    user_id : str
        Owner user identifier
    created_at : int
        Unix timestamp of creation
    updated_at : int
        Unix timestamp of last update
    access_control : AccessControl
        Access control configuration
    user : User
        Owner user information
    write_access : bool
        Whether current user has write access
    meta : Optional[Dict[str, Any]]
        Additional metadata
    files : Optional[List[File]]
        List of files in the knowledge base (present in detailed view)
    """

    id: str
    name: str
    description: str
    user_id: str
    created_at: int
    updated_at: int
    access_control: AccessControl
    user: Optional[User] = None
    write_access: bool
    meta: Optional[Dict[str, Any]] = None
    files: Optional[List[File]] = Field(default_factory=list)


def validate_response(
    data: Any, model_class: type[BaseModel], context: str = "API response"
) -> Optional[BaseModel]:
    """Validate API response data against a Pydantic model.

    This function attempts to validate the response data. If validation fails,
    it logs a warning with details but does not raise an exception, allowing
    the application to continue with degraded functionality.

    Parameters
    ----------
    data : Any
        Raw data to validate (typically from API response)
    model_class : type[BaseModel]
        Pydantic model class to validate against
    context : str
        Description of what's being validated (for logging)

    Returns
    -------
    Optional[BaseModel]
        Validated model instance if successful, None if validation failed
    """
    try:
        return model_class.model_validate(data)
    except ValidationError as e:
        logger.warning(f"Validation failed for {context}: {model_class.__name__}")
        logger.warning(f"Validation errors: {e}")
        logger.debug(f"Invalid data: {data}")
        return None
