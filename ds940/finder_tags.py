"""macOS Finder extended attributes, native color tags, and Spotlight metadata."""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
from pathlib import Path
import plistlib
import re
import sys

LOG = logging.getLogger(__name__)

# Standard macOS Finder color codes
FINDER_COLOR_NONE = 0
FINDER_COLOR_GRAY = 1
FINDER_COLOR_GREEN = 2
FINDER_COLOR_PURPLE = 3
FINDER_COLOR_BLUE = 4
FINDER_COLOR_YELLOW = 5
FINDER_COLOR_RED = 6
FINDER_COLOR_ORANGE = 7

# Extended attribute keys
XATTR_USER_TAGS = b"com.apple.metadata:_kMDItemUserTags"
XATTR_FINDER_INFO = b"com.apple.FinderInfo"
XATTR_KEYWORDS = b"com.apple.metadata:kMDItemKeywords"
XATTR_TITLE = b"com.apple.metadata:kMDItemTitle"

_libc = None
_setxattr = None
_getxattr = None


def _init_libc():
    global _libc, _setxattr, _getxattr
    if sys.platform != "darwin":
        return None
    if _libc is None:
        try:
            lib_path = ctypes.util.find_library("c")
            if lib_path:
                _libc = ctypes.CDLL(lib_path)
                # macOS signature: setxattr(path, name, value, size, position, options)
                _libc.setxattr.argtypes = [
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                    ctypes.c_void_p,
                    ctypes.c_size_t,
                    ctypes.c_uint32,
                    ctypes.c_int,
                ]
                _libc.setxattr.restype = ctypes.c_int

                # macOS signature: getxattr(path, name, value, size, position, options)
                _libc.getxattr.argtypes = [
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                    ctypes.c_void_p,
                    ctypes.c_size_t,
                    ctypes.c_uint32,
                    ctypes.c_int,
                ]
                _libc.getxattr.restype = ctypes.c_ssize_t
                _setxattr = _libc.setxattr
                _getxattr = _libc.getxattr
        except Exception as err:
            LOG.warning("Could not initialize macOS libc xattr functions: %s", err)
    return _libc


def get_finder_color(category: str) -> int:
    """Return the native macOS Finder color code based on document category."""
    cat = (category or "").lower()
    if any(k in cat for k in ("tax", "warrant", "urgent", "delinquent")):
        return FINDER_COLOR_RED
    if any(k in cat for k in ("legal", "court", "citation", "law", "attorney")):
        return FINDER_COLOR_PURPLE
    if any(k in cat for k in ("bank", "finance", "bill", "invoice", "receipt", "payment", "payroll")):
        return FINDER_COLOR_GREEN
    if any(k in cat for k in ("medic", "health", "doctor", "lab", "hospital", "clinic", "dental", "rx")):
        return FINDER_COLOR_BLUE
    if any(k in cat for k in ("auto", "vehicle", "car", "dmv", "txdmv", "insurance")):
        return FINDER_COLOR_ORANGE
    if any(k in cat for k in ("utilit", "energy", "water", "electric", "internet", "telecom")):
        return FINDER_COLOR_YELLOW
    return FINDER_COLOR_GRAY


def set_finder_metadata(
    filepath: Path | str,
    category: str = "",
    tags: list[str] | None = None,
    title: str = "",
    keywords: list[str] | None = None,
    color: int | None = None,
) -> bool:
    """Set native macOS Finder tags, color label, Spotlight keywords, and title.

    Safely no-ops on non-Darwin platforms (Linux, Docker).
    """
    if sys.platform != "darwin":
        return False

    _init_libc()
    if _setxattr is None:
        return False

    path_obj = Path(filepath).resolve()
    if not path_obj.exists():
        return False

    path_b = str(path_obj).encode("utf-8")
    target_color = color if color is not None else get_finder_color(category)

    try:
        # 1. Assemble User Tags
        all_tags = []
        if category:
            all_tags.append(f"{category}\n{target_color}")

        if tags:
            for t in tags:
                clean_t = str(t).strip()
                if not clean_t:
                    continue
                # If tag already has color newline, keep it, otherwise add \n0
                if "\n" in clean_t:
                    if clean_t not in all_tags:
                        all_tags.append(clean_t)
                else:
                    entry = f"{clean_t}\n0"
                    if clean_t != category and entry not in all_tags:
                        all_tags.append(entry)

        if all_tags:
            tags_plist = plistlib.dumps(all_tags, fmt=plistlib.FMT_BINARY)
            _setxattr(path_b, XATTR_USER_TAGS, tags_plist, len(tags_plist), 0, 0)

        # 2. Set FinderInfo 32-byte struct for instant colored dot in Finder
        if target_color > 0:
            finder_info = bytearray(32)
            finder_info[9] = (target_color & 0x07) * 2
            _setxattr(path_b, XATTR_FINDER_INFO, bytes(finder_info), 32, 0, 0)

        # 3. Spotlight Keywords
        all_kw = []
        if category and category not in all_kw:
            all_kw.append(category)
        if keywords:
            for kw in keywords:
                if kw and kw not in all_kw:
                    all_kw.append(kw)

        if all_kw:
            kw_plist = plistlib.dumps(all_kw, fmt=plistlib.FMT_BINARY)
            _setxattr(path_b, XATTR_KEYWORDS, kw_plist, len(kw_plist), 0, 0)

        # 4. Spotlight Document Title
        if title:
            clean_title = title.encode("utf-8")
            _setxattr(path_b, XATTR_TITLE, clean_title, len(clean_title), 0, 0)

        return True
    except Exception as err:
        LOG.warning("Failed setting Finder metadata on %s: %s", path_obj.name, err)
        return False


def get_finder_metadata(filepath: Path | str) -> dict:
    """Read back native macOS Finder tags, color code, keywords, and title."""
    if sys.platform != "darwin":
        return {"tags": [], "color": 0, "keywords": [], "title": ""}

    _init_libc()
    if _getxattr is None:
        return {"tags": [], "color": 0, "keywords": [], "title": ""}

    path_obj = Path(filepath).resolve()
    if not path_obj.exists():
        return {"tags": [], "color": 0, "keywords": [], "title": ""}

    path_b = str(path_obj).encode("utf-8")
    result: dict = {"tags": [], "color": 0, "keywords": [], "title": ""}

    buf = ctypes.create_string_buffer(8192)

    # 1. User Tags
    try:
        size = _getxattr(path_b, XATTR_USER_TAGS, buf, len(buf), 0, 0)
        if size > 0:
            raw_plist = buf.raw[:size]
            tags_list = plistlib.loads(raw_plist)
            clean_tags = []
            for t in tags_list:
                parts = str(t).split("\n", 1)
                clean_tags.append(parts[0])
                if len(parts) > 1 and parts[1].isdigit() and result["color"] == 0:
                    result["color"] = int(parts[1])
            result["tags"] = clean_tags
    except Exception:
        pass

    # 2. FinderInfo
    try:
        fbuf = ctypes.create_string_buffer(32)
        fsize = _getxattr(path_b, XATTR_FINDER_INFO, fbuf, 32, 0, 0)
        if fsize >= 10:
            raw_byte = fbuf.raw[9]
            color_code = (raw_byte >> 1) & 0x07
            if color_code > 0:
                result["color"] = color_code
    except Exception:
        pass

    # 3. Keywords
    try:
        ksize = _getxattr(path_b, XATTR_KEYWORDS, buf, len(buf), 0, 0)
        if ksize > 0:
            result["keywords"] = list(plistlib.loads(buf.raw[:ksize]))
    except Exception:
        pass

    # 4. Title
    try:
        tsize = _getxattr(path_b, XATTR_TITLE, buf, len(buf), 0, 0)
        if tsize > 0:
            result["title"] = buf.raw[:tsize].decode("utf-8", errors="replace")
    except Exception:
        pass

    return result


def tag_all_scans(root_dir: Path | str) -> list[dict]:
    """Recursively tag all PDF documents in root_dir with native Finder metadata."""
    root = Path(root_dir).resolve()
    if not root.is_dir():
        return []

    results = []
    for path in root.rglob("*.pdf"):
        if path.name.startswith("."):
            continue

        rel_parent = path.relative_to(root).parent
        category = rel_parent.name if rel_parent.name else "Inbox"

        # Extract title from filename (e.g. 2026-10-02_Austin-Municipal-Court_Notice.pdf)
        base = path.stem
        date_m = re.match(r"^\d{4}-\d{2}-\d{2}_(.*)$", base)
        title = date_m.group(1).replace("_", " ") if date_m else base.replace("_", " ")

        success = set_finder_metadata(
            path,
            category=category,
            tags=[category, "Brother DS-940DW"],
            title=title,
            keywords=[category, "scanned", "brother-ds940dw"],
        )
        results.append({
            "path": str(path),
            "filename": path.name,
            "category": category,
            "color": get_finder_color(category),
            "success": success,
        })

    return results
