#!/usr/bin/env python
# -*- coding: utf8 -*-
"""
PowerScale file scanner
"""
# fmt: off
__title__         = "ps_scan_api_server"
__version__       = "0.1.0"
__date__          = "25 September 2023"
__license__       = "MIT"
__author__        = "Andrew Chung <andrew.chung@dell.com>"
__maintainer__    = "Andrew Chung <andrew.chung@dell.com>"
__email__         = "andrew.chung@dell.com"
# fmt: on
import datetime
import errno
import gzip
import io
import json
import logging
from logging.config import dictConfig
import os
import re
import signal
import stat
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "libs"))


dictConfig(
    {
        "version": 1,
        "formatters": {
            "default": {
                "format": "%(asctime)s - %(levelname)s - [%(module)s:%(lineno)d] - %(message)s",
            }
        },
        "handlers": {
            "wsgi": {
                "class": "logging.StreamHandler",
                "stream": "ext://flask.logging.wsgi_errors_stream",
                "formatter": "default",
            }
        },
        "root": {"level": "INFO", "handlers": ["wsgi"]},
    }
)

import helpers.cli_parser_api as cli_parser
from helpers.constants import *
import helpers.misc as misc
from libs.flask import Flask
from libs.flask import make_response
from libs.flask import request
from libs.flask import Response
from libs.simple_cache import SimpleCache
from libs.waitress import create_server

try:
    import isi.fs.attr as attr
    import isi.fs.userattr as uattr
    import libs.onefs_acl as onefs_acl
    from libs.onefs_auth import translate_user_group_perms
    from libs.onefs_become_user import become_user
except:
    pass
try:
    dir(PermissionError)
except:
    PermissionError = Exception
try:
    dir(FileNotFoundError)
except:
    FileNotFoundError = IOError

app = Flask(__name__)
server = None

ENCODING_GZIP = "gzip"
ENCODING_DEFLATE = "deflate"
LOG = app.logger
HTTP_HDR_ACCEPT_ENCODING = "Accept-Encoding"
HTTP_HDR_CONTENT_ENCODING = "Content-Encoding"
HTTP_HDR_CONTENT_LEN = "Content-Length"
JSON_SER_ERR = "<not serializable>"
MIME_TYPE_JSON = "application/json"
REQUIRED_RETURN_FIELDS = [
    "atime",
    "ctime",
    "file_ext",
    "file_hard_links",
    "file_name",
    "file_path",
    "file_type",
    "inode",
    "mtime",
    "perms_group",
    "perms_unix_gid",
    "perms_unix_uid",
    "perms_user",
    "size",
    "size_logical",
    "size_physical",
]
TXT_INVALID_TOKEN = "Invalid continuation token received. Either the token does not exist or the token has expired"
TXT_QUERY_PATH_REQUIRED = "A URL encoded path is required in the 'path' query parameter"


def add_diskover_fields(file_info, remove_existing=True):
    diskover_info = {
        "atime": file_info["atime"],
        "ctime": file_info["ctime"],
        "extension": file_info["file_ext"],
        "group": file_info["perms_unix_gid"],
        "ino": file_info["inode"],
        "mtime": file_info["mtime"],
        "name": file_info["file_name"],
        "nlink": file_info["file_hard_links"],
        "owner": file_info["perms_unix_uid"],
        "parent_path": file_info["file_path"],
        "size": file_info["size"],
        "size_du": file_info["size_physical"],
        "type": file_info["file_type"],
        "pscale": file_info,
    }
    if remove_existing:
        for field in [
            "atime",
            "ctime",
            "file_ext",
            "file_hard_links",
            "file_name",
            "file_path",
            "file_type",
            "inode",
            "mtime",
            "perms_group",
            "perms_user",
            "size",
            "size_physical",
        ]:
            if field in file_info:
                del file_info[field]
    return diskover_info


def convert_response_to_diskover(resp_data):
    now = time.time()
    dirs_list = resp_data["contents"]["dirs"]
    files_list = resp_data["contents"]["files"]
    root = resp_data["contents"].get("root", {})
    stats = resp_data["statistics"]
    resp_data["contents"] = {"entries": [], "root": root}
    entries = resp_data["contents"]["entries"]
    skipped_files = 0
    for i in range(len(files_list)):
        if files_list[i]["file_type"] != "file":
            skipped_files += 1
            continue
        entries.append(add_diskover_fields(files_list[i]))
    for i in range(len(dirs_list)):
        entries.append(add_diskover_fields(dirs_list[i]))
    if root:
        resp_data["contents"]["root"] = add_diskover_fields(root)
    stats["skipped"] += skipped_files
    stats["processed"] -= skipped_files
    stats["time_conversion"] = time.time() - now
    return resp_data


def file_handler_pscale(root, filename_list, args={}):
    """Gets the metadata for the files/directories based at root and given the file/dir names in filename_list

    Parameters
    ----------
    root: <string> Root directory to start the scan
    filename_list: <list:string> List of file and directory names to retrieve metadata
    args: <dict> Dictionary containing parameters to control the scan
            {
              "custom_tagging": <bool>          # When true call a custom handler for each file
              "extra_attr": <bool>              # When true, gets extra OneFS metadata
              "no_acl": <bool>                  # When true, skip ACL parsing
              "phys_block_size": <int>          # Number of bytes in a block for the underlying storage device
              "nodepool_translation": <dict>    # Dictionary with a node pool number to text string translation
              "strip_dot_snapshot": <bool>      # When true, strip the .snapshot name from the file path returned
              "user_attr": <bool>               # When true, get user attribute data for files
            }

    Returns
    ----------
    dict - A dictionary representing the root and files scanned
            {
              "dirs": [<dict>]                  # List of directory metadata objects
              "files": [<dict>]                 # List of file metadata objects
              "statistics": {
                "lstat_required": <bool>        # Number of times lstat was called vs. internal stat call
                "not_found": <int>              # Number of files that were not found
                "processed": <int>              # Number of files actually processed
                "skipped": <int>                # Number of files skipped
                "time_access_time": <int>       # Seconds spent getting the file access time
                "time_acl": <int>               # Seconds spent getting file ACL
                "time_custom_tagging": <int>    # Seconds spent processing custom tags
                "time_dinode": <int>            # Seconds spent getting OneFS metadata
                "time_extra_attr": <int>        # Seconds spent getting extra OneFS metadata
                "time_lstat": <int>             # Seconds spent in lstat
                "time_scan_dir": <int>          # Seconds spent scanning the entire directory
                "time_user_attr": <int>         # Seconds spent scanning user attributes
              }
            }
    """
    now = time.time()
    custom_tagging = args.get("custom_tagging", False)
    extra_attr = args.get("extra_attr", DEFAULT_PARSE_EXTRA_ATTR)
    filter_fields = args.get("fields")
    no_acl = args.get("no_acl", DEFAULT_PARSE_SKIP_ACLS)
    phys_block_size = args.get("phys_block_size", IFS_BLOCK_SIZE)
    pool_translate = args.get("nodepool_translation", {})
    strip_dot_snapshot = args.get("strip_dot_snapshot", DEFAULT_STRIP_DOT_SNAPSHOT)
    user_attr = args.get("user_attr", DEFAULT_PARSE_USER_ATTR)

    result_list = []
    result_dir_list = []
    stats = {
        "lstat_required": 0,
        "not_found": 0,
        "processed": 0,
        "skipped": 0,
        "time_access_time": 0,
        "time_acl": 0,
        "time_custom_tagging": 0,
        "time_dinode": 0,
        "time_extra_attr": 0,
        "time_lstat": 0,
        "time_scan_dir": 0,
        "time_user_attr": 0,
    }

    for filename in filename_list:
        try:
            full_path = os.path.join(root, filename)
            fd = None
            try:
                fd = os.open(full_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_OPENLINK)
            except FileNotFoundError:
                LOG.debug({"msg": "File not found", "file_path": full_path})
                stats["not_found"] += 1
                continue
            except Exception as e:
                if e.errno in (errno.ENOTSUP, errno.EACCES):  # 45: Not supported, 13: No access
                    stats["lstat_required"] += 1
                    LOG.debug({"msg": "Unable to call os.open. Using os.lstat instead", "file_path": full_path})
                    time_start = time.time()
                    file_info = get_file_stat(root, filename, phys_block_size, strip_dot_snapshot=strip_dot_snapshot)
                    stats["time_lstat"] += time.time() - time_start
                    if custom_tagging:
                        time_start = time.time()
                        file_info["user_tags"] = custom_tagging(file_info)
                        stats["time_custom_tagging"] += time.time() - time_start
                    if file_info["file_type"] == "dir":
                        result_dir_list.append(file_info)
                        # Fix size issues with dirs
                        file_info["size_logical"] = 0
                        stats["processed"] += 1
                        continue
                    # Filter out keys if requested
                    if filter_fields:
                        for key in list(file_info.keys()):
                            if key not in filter_fields:
                                del file_info[key]
                    translate_user_group_perms(full_path, file_info)
                    result_list.append(file_info)
                    stats["processed"] += 1
                    continue
                LOG.exception({"msg": "Error found when calling os.open", "file_path": full_path, "error": str(e)})
                continue
            time_start = time.time()
            fstats = attr.get_dinode(fd)
            stats["time_dinode"] += time.time() - time_start
            # atime call can return empty if the file does not have an atime or atime tracking is disabled
            time_start = time.time()
            atime = attr.get_access_time(fd)
            stats["time_access_time"] += time.time() - time_start
            if atime:
                atime = atime[0]
            else:
                # If atime does not exist, use the last metadata change time as this captures the last time someone
                # modified either the data or the inode of the file
                atime = fstats["di_ctime"]
            di_data_blocks = fstats.get("di_data_blocks", fstats["di_physical_blocks"] - fstats["di_protection_blocks"])
            logical_blocks = fstats["di_logical_size"] // phys_block_size
            comp_blocks = logical_blocks - fstats["di_shadow_refs"]
            compressed_file = True if (di_data_blocks and comp_blocks) else False
            stubbed_file = (fstats["di_flags"] & IFLAG_COMBO_STUBBED) > 0
            if strip_dot_snapshot:
                file_path = re.sub(RE_STRIP_SNAPSHOT, "", root, count=1)
            else:
                file_path = root
            file_info = {
                # ========== Timestamps ==========
                "atime": atime,
                "atime_date": datetime.date.fromtimestamp(atime).isoformat(),
                "btime": fstats["di_create_time"],
                "btime_date": datetime.date.fromtimestamp(fstats["di_create_time"]).isoformat(),
                "ctime": fstats["di_ctime"],
                "ctime_date": datetime.date.fromtimestamp(fstats["di_ctime"]).isoformat(),
                "mtime": fstats["di_mtime"],
                "mtime_date": datetime.date.fromtimestamp(fstats["di_mtime"]).isoformat(),
                # ========== File and path strings ==========
                "file_path": file_path,
                "file_name": filename,
                "file_ext": os.path.splitext(filename)[1],
                # ========== File attributes ==========
                "file_access_pattern": ACCESS_PATTERN[fstats["di_la_pattern"]],
                "file_compression_ratio": comp_blocks / di_data_blocks if compressed_file else 1,
                "file_hard_links": fstats["di_nlink"],
                "file_is_ads": ((fstats["di_flags"] & IFLAGS_UF_HASADS) != 0),
                "file_is_compressed": (comp_blocks > di_data_blocks) if compressed_file else False,
                "file_is_dedupe_disabled": not not fstats["di_no_dedupe"],
                "file_is_deduped": (fstats["di_shadow_refs"] > 0),
                "file_is_inlined": (
                    (fstats["di_physical_blocks"] == 0)
                    and (fstats["di_shadow_refs"] == 0)
                    and (fstats["di_logical_size"] > 0)
                ),
                "file_is_packed": not not fstats["di_packing_policy"],
                "file_is_smartlinked": stubbed_file,
                "file_is_sparse": ((fstats["di_logical_size"] < fstats["di_size"]) and not stubbed_file),
                "file_type": FILE_TYPE[fstats["di_mode"] & FILE_TYPE_MASK],
                "inode": fstats["di_lin"],
                "inode_mirror_count": fstats["di_inode_mc"],
                "inode_parent": fstats["di_parent_lin"],
                "inode_revision": fstats["di_rev"],
                # ========== Storage pool targets ==========
                "pool_target_data": fstats["di_data_pool_target"],
                "pool_target_data_name": pool_translate.get(
                    int(fstats["di_data_pool_target"]), str(fstats["di_data_pool_target"])
                ),
                "pool_target_metadata": fstats["di_metadata_pool_target"],
                "pool_target_metadata_name": pool_translate.get(
                    int(fstats["di_metadata_pool_target"]), str(fstats["di_metadata_pool_target"])
                ),
                # ========== Permissions ==========
                "perms_unix_bitmask": stat.S_IMODE(fstats["di_mode"]),
                "perms_unix_gid": fstats["di_gid"],
                "perms_unix_uid": fstats["di_uid"],
                # ========== File protection level ==========
                "protection_current": fstats["di_current_protection"],
                "protection_target": fstats["di_protection_policy"],
                # ========== File allocation size and blocks ==========
                # The apparent size of the file. Sparse files include the sparse area
                "size": fstats["di_size"],
                # Logical size in 8K blocks. Sparse files only show the real data portion
                "size_logical": fstats["di_logical_size"],
                # Physical size on disk including protection overhead, including extension blocks and excluding metadata
                "size_physical": fstats["di_physical_blocks"] * phys_block_size,
                # Physical size on disk excluding protection overhead and excluding metadata
                "size_physical_data": di_data_blocks * phys_block_size,
                # Physical size on disk of the protection overhead
                "size_protection": fstats["di_protection_blocks"] * phys_block_size,
                # ========== SSD usage ==========
                "ssd_strategy": fstats["di_la_ssd_strategy"],
                "ssd_strategy_name": SSD_STRATEGY[fstats["di_la_ssd_strategy"]],
                "ssd_status": fstats["di_la_ssd_status"],
                "ssd_status_name": SSD_STATUS[fstats["di_la_ssd_status"]],
            }
            if not no_acl:
                time_start = time.time()
                acl = onefs_acl.get_acl_dict(fd)
                stats["time_acl"] += time.time() - time_start
                file_info["perms_acl_aces"] = misc.ace_list_to_str_list(acl.get("aces"))
                file_info["perms_acl_group"] = misc.acl_group_to_str(acl)
                file_info["perms_acl_user"] = misc.acl_user_to_str(acl)
            if extra_attr:
                # di_flags may have other bits we need to translate
                #     Coalescer setting (on|off|endurant all|coalescer only)
                #     IFLAGS_UF_WRITECACHE and IFLAGS_UF_WC_ENDURANT flags
                # Do we want inode locations? how many on SSD and spinning disk?
                #   - Get data from estats["ge_iaddrs"], e.g. ge_iaddrs: [(1, 13, 1098752, 512)]
                # Extended attributes/custom attributes?
                time_start = time.time()
                estats = attr.get_expattr(fd)
                stats["time_extra_attr"] += time.time() - time_start
                # Add up all the inode sizes
                metadata_size = 0
                for inode in estats["ge_iaddrs"]:
                    metadata_size += inode[3]
                # Sum of the size of all the inodes. This includes inodes that mix both 512 byte and 8192 byte inodes
                file_info["size_metadata"] = metadata_size
                file_info["file_is_manual_access"] = not not estats["ge_manually_manage_access"]
                file_info["file_is_manual_packing"] = not not estats["ge_manually_manage_packing"]
                file_info["file_is_manual_protection"] = not not estats["ge_manually_manage_protection"]
                if estats["ge_coalescing_ec"] & estats["ge_coalescing_on"]:
                    file_info["file_coalescer"] = "coalescer on, ec off"
                elif estats["ge_coalescing_on"]:
                    file_info["file_coalescer"] = "coalescer on, ec on"
                elif estats["ge_coalescing_ec"]:
                    file_info["file_coalescer"] = "coalescer off, ec on"
                else:
                    file_info["file_coalescer"] = "coalescer off, ec off"
            if user_attr:
                extended_attr = {}
                time_start = time.time()
                keys = uattr.userattr_list(fd)
                for key in keys:
                    extended_attr[key] = uattr.userattr_get(fd, key)
                stats["time_user_attr"] += time.time() - time_start
                file_info["user_attributes"] = extended_attr
            if custom_tagging:
                time_start = time.time()
                file_info["user_tags"] = custom_tagging(file_info)
                stats["time_custom_tagging"] += time.time() - time_start

            time_start = time.time()
            lstat_required = translate_user_group_perms(full_path, file_info)
            if lstat_required:
                stats["lstat_required"] += 1
                stats["time_lstat"] += time.time() - time_start

            # Filter out keys if requested
            if filter_fields:
                for key in list(file_info.keys()):
                    if key not in filter_fields:
                        del file_info[key]
            if fstats["di_mode"] & 0o040000:
                result_dir_list.append(file_info)
                # Fix size issues with dirs
                file_info["size_logical"] = 0
                stats["processed"] += 1
                continue
            result_list.append(file_info)
            if (
                (fstats["di_mode"] & 0o010000 == 0o010000)
                or (fstats["di_mode"] & 0o120000 == 0o120000)
                or (fstats["di_mode"] & 0o140000 == 0o140000)
            ):
                # Fix size issues with symlinks, sockets, and FIFOs
                file_info["size_logical"] = 0
            stats["processed"] += 1
        except IOError as ioe:
            stats["skipped"] += 1
            if ioe.errno == errno.EACCES:  # 13: No access
                LOG.warn({"msg": "Permission error", "file_path": full_path})
            else:
                LOG.exception(ioe)
        except FileNotFoundError as fnfe:
            stats["not_found"] += 1
            LOG.warn({"msg": "File not found", "file_path": full_path})
        except PermissionError as pe:
            stats["skipped"] += 1
            LOG.warn({"msg": "Permission error", "file_path": full_path})
            LOG.exception(pe)
        except Exception as e:
            stats["skipped"] += 1
            LOG.warn({"msg": "General exception", "file_path": full_path})
            LOG.exception(e)
        finally:
            try:
                os.close(fd)
            except:
                pass
    stats["time_scan_dir"] = time.time() - now
    results = {
        "dirs": result_dir_list,
        "files": result_list,
        "statistics": stats,
    }
    return results


def get_file_stat(root, filename, block_unit=STAT_BLOCK_SIZE, strip_dot_snapshot=True):
    full_path = os.path.join(root, filename)
    fstats = os.lstat(full_path)
    if strip_dot_snapshot:
        file_path = re.sub(RE_STRIP_SNAPSHOT, "", root, count=1)
    else:
        file_path = root
    file_info = {
        # ========== Timestamps ==========
        "atime": fstats.st_atime,
        "atime_date": datetime.date.fromtimestamp(fstats.st_atime).isoformat(),
        "btime": None,
        "btime_date": None,
        "ctime": fstats.st_ctime,
        "ctime_date": datetime.date.fromtimestamp(fstats.st_ctime).isoformat(),
        "mtime": fstats.st_mtime,
        "mtime_date": datetime.date.fromtimestamp(fstats.st_mtime).isoformat(),
        # ========== File and path strings ==========
        "file_path": file_path,
        "file_name": filename,
        "file_ext": os.path.splitext(filename),
        # ========== File attributes ==========
        "file_hard_links": fstats.st_nlink,
        "file_type": FILE_TYPE[stat.S_IFMT(fstats.st_mode) & FILE_TYPE_MASK],
        "inode": fstats.st_ino,
        # ========== Permissions ==========
        "perms_unix_bitmask": stat.S_IMODE(fstats.st_mode),
        "perms_unix_gid": fstats.st_gid,
        "perms_unix_uid": fstats.st_uid,
        # ========== File allocation size and blocks ==========
        "size": fstats.st_size,
        "size_logical": block_unit * (int(fstats.st_size / block_unit) + 1 * ((fstats.st_size % block_unit) > 0)),
        # st_blocks includes metadata blocks
        "size_physical": block_unit * (int(fstats.st_blocks * STAT_BLOCK_SIZE / block_unit)),
    }
    try:
        file_info["btime"] = fstats.st_birthtime
        file_info["btime_date"] = datetime.date.fromtimestamp(fstats.st_btime).isoformat()
    except:
        # No birthtime date so do not add those fields
        pass
    if file_info["size"] == 0 and file_info["size_physical"] == 0:
        file_info["size_physical"] = file_info["size_logical"]
    return file_info


def signal_handler(signum, frame):
    global server
    if signum in [signal.SIGINT, signal.SIGTERM]:
        server.close()
        # Cleanup SimpleCache
        cache = app.config.get("cache")
        if cache:
            del app.config["cache"]
            cache.__del__()
        sys.exit(0)
    if signum in [signal.SIGUSR1]:
        root_logger = logging.getLogger("")
        cur_level = root_logger.getEffectiveLevel()
        if cur_level != logging.DEBUG:
            root_logger.setLevel(logging.DEBUG)
        else:
            root_logger.setLevel(logging.INFO)
        LOG.critical(
            {
                "msg": "SIGUSR1 signal received. Toggling debug.",
                "prev_state": cur_level,
                "next_state": root_logger.getEffectiveLevel(),
            }
        )


@app.after_request
def compress(response):
    # 0: No compression, 1: Fastest, 9: Slowest
    compress_level = 9
    accept_encoding = request.headers.get(HTTP_HDR_ACCEPT_ENCODING, "").lower()
    if (
        response.status_code < 200
        or response.status_code >= 300
        or response.direct_passthrough
        or ((ENCODING_GZIP not in accept_encoding) and (ENCODING_DEFLATE not in accept_encoding))
        or HTTP_HDR_CONTENT_ENCODING in response.headers
    ):
        return response
    # Prefer gzip over deflate when available
    if ENCODING_GZIP in accept_encoding:
        buffer = io.BytesIO()
        with gzip.GzipFile(mode="wb", compresslevel=compress_level, fileobj=buffer) as gz_file:
            gz_file.write(response.get_data())
        content = buffer.getvalue()
        encoding = ENCODING_GZIP
    elif ENCODING_DEFLATE in accept_encoding:
        content = gzip.zlib.compress(response.get_data(), compress_level)
        encoding = ENCODING_DEFLATE
    response.set_data(content)
    response.headers[HTTP_HDR_CONTENT_LEN] = len(content)
    response.headers[HTTP_HDR_CONTENT_ENCODING] = encoding
    return response


@app.route("/cluster_storage_stats", methods=["GET"])
def handle_cluster_storage_stats():
    args = request.args
    storage_usage_stats = {}
    if misc.is_onefs_os():
        storage_usage_stats = misc.get_local_storage_usage_stats()
    else:
        # TODO: Add support for querying for usage statistics
        pass
    resp = Response(json.dumps(storage_usage_stats), mimetype=MIME_TYPE_JSON)
    return resp


@app.route("/ps_stat/list", methods=["GET"])
def handle_ps_stat_list():
    """Returns metadata for both the root path and all the immediate children of that path
    In the case the root is a file, only the file metadata will be returned

    Query arguments (common)
    ----------
    fields: <string> Comma separated string with a complete list of field names to return. Defaults to the empty string
            which returns all fields.
    limit: <int> Maximum number of entries to return in a single call. Defaults to 10000. Maximum value of 100000
    path: <string> URL encoded string representing the path in the file system that metadata will be returned
            The path can start with /ifs or not. The /ifs part of the path will be prepended if necessary
            A path with a trailing slash will have the slash removed.
    token: <string> Token string to allow the continuation of a previous scan request when that request did not return
            all the available data for a specific root path. Tokens expire and using an expired token results in a 404
    type: <string> Type of scan result to return. One of: powerscale|diskover. The default is powerscale.

    Query arguments (optional)
    ----------
    custom_tagging: <bool> When true call a custom handler for each file. Enabling this can slow down scan speed
    extra_attr: <bool> When true, gets extra OneFS metadata. Enabling this can slow down scan speed
    include_root: <bool> When true, the metadata for the path specified in the path query parameter will be returned
            in the "contents" object under the key "root"
    no_acl: <bool> When true, skip ACL parsing. Enabling this can speed up scanning but results will not have ACLs
    strip_dot_snapshot: <bool> When true, strip the .snapshot name from the file path returned
    user_attr: <bool> # When true, get user attribute data for files. Enabling this can slow down scan speed

    A bool value is false if the value is 0 or the string false. Any other value is interpreted as a true value.

    Returns
    ----------
    dict - A dictionary representing the root and files scanned
        {
          "contents": {
            "dirs": [<dict>]                  # List of directory metadata objects
            "files": [<dict>]                 # List of file metadata objects
            "root": <dict>                    # Metadata object for the root path
          }
          "items_total": <int>                # Total number of items remaining that could be returned
          "items_returned": <int>             # Number of metadata items returned. This number includes the "root"
          "token_continuation": <string>      # String that should be used in the "token" query argument to continue
                                              # scanning a directory
          "token_expiration": <int>           # Epoch seconds specifying when the token will expire
          "statistics": {
            "lstat_required": <bool>          # Number of times lstat was called vs. internal stat call
            "not_found": <int>                # Number of files that were not found
            "processed": <int>                # Number of files actually processed
            "skipped": <int>                  # Number of files skipped
            "time_access_time": <int>         # Seconds spent getting the file access time
            "time_acl": <int>                 # Seconds spent getting file ACL
            "time_custom_tagging": <int>      # Seconds spent processing custom tags
            "time_dinode": <int>              # Seconds spent getting OneFS metadata
            "time_extra_attr": <int>          # Seconds spent getting extra OneFS metadata
            "time_lstat": <int>               # Seconds spent in lstat
            "time_scan_dir": <int>            # Seconds spent scanning the entire directory
            "time_user_attr": <int>           # Seconds spent scanning user attributes
          }
        }
    """
    args = request.args
    options = app.config["ps_scan"]["options"]
    param = {
        "custom_tagging": misc.parse_arg_bool(args, "custom_tagging", False),
        "extra_attr": misc.parse_arg_bool(args, "extra_attr", False),
        "include_root": misc.parse_arg_bool(args, "include_root", False),
        "fields": str(args.get("fields", "")),
        "limit": misc.parse_arg_int(args, "limit", options["default_item_limit"], 1, options["max_item_limit"]),
        "no_acl": misc.parse_arg_bool(args, "no_acl", False),
        "nodepool_translation": app.config["ps_scan"]["nodepool_translation"],
        "path": args.get("path"),
        "strip_do_snapshot": misc.parse_arg_bool(args, "strip_dot_snapshot", True),
        "token": args.get("token"),
        "type": args.get("type", DEFAULT_DATA_TYPE),
        "user_attr": misc.parse_arg_bool(args, "user_attr", False),
    }
    if not param["path"]:
        return make_response({"msg": TXT_QUERY_PATH_REQUIRED}, 404)
    if param["fields"]:
        # Parse fields to return
        fields = param["fields"].split(",")
        param["fields"] = []
        for field in fields:
            if not re.match(r"[^a-zA-Z0-9_\-,.]", field):
                param["fields"].append(field)
        # Ensure we have a minimum set of fields
        if param["fields"]:
            param["fields"] = list(set(param["fields"] + REQUIRED_RETURN_FIELDS))

    dir_list = []
    dir_list_len = 0
    list_stat_data = {}
    root = {}
    root_is_dir = False
    stat_data = None
    token_continuation = ""
    token_expiration = ""

    # Get the base directory, the last path component, and the full path. e.g. /ifs, foo, /ifs/foo
    base, file, full_path = misc.get_path_from_urlencoded(param["path"])
    if not param["token"]:
        if param["include_root"]:
            stat_data = file_handler_pscale(base, [file], param)
            if stat_data["dirs"]:
                root = stat_data["dirs"][0]
                root_is_dir = True
                param["limit"] -= 1
            elif stat_data["files"]:
                root = stat_data["files"][0]
                param["limit"] -= 1
        else:
            root_is_dir = True

    if root_is_dir or param["token"]:
        # Get the list of files/directories to process either from the file system directly or a cache
        if param["token"]:
            # Get the cached list and then set dir_list
            LOG.debug({"msg": "Getting cached directory listing", "path": full_path})
            cached_item = app.config["cache"].get_item(param["token"])
            if not cached_item:
                return make_response({"msg": TXT_INVALID_TOKEN, "token": param["token"]}, 404)
            base = cached_item["base"]
            dir_list = cached_item["dir_list"]
            offset = cached_item["offset"] + param["limit"]
            LOG.debug({"msg": "Cached directory listing complete", "path": full_path})
        else:
            # Process the direct children of the passed in path
            LOG.debug({"msg": "Getting directory listing", "path": full_path})
            dir_list = misc.get_directory_listing(full_path)
            LOG.debug({"msg": "Directory listing complete", "path": full_path})
            offset = param["limit"]
        # Split this list up into chunks dependent on the 'limit' query value
        dir_list_len = len(dir_list)
        if dir_list_len > param["limit"]:
            # Cache the remainder of the directory listing to avoid re-scanning the directory
            token_data = app.config["cache"].add_item(
                {"base": full_path, "dir_list": dir_list[param["limit"] :], "offset": offset}
            )
            dir_list = dir_list[0 : param["limit"]]
            token_continuation = token_data["token"]
            token_expiration = token_data["expiration"]
        # Perform the actual stat commands on each file/directory
        LOG.debug({"msg": "Parsing directory", "path": full_path})
        list_stat_data = file_handler_pscale(full_path, dir_list, param)
        LOG.debug({"msg": "Parsing complete", "path": full_path})

    # Calculate statistics to return in the response
    dirs_len = len(list_stat_data.get("dirs", []))
    files_len = len(list_stat_data.get("files", []))
    items_total = dir_list_len + 1 * (not not root)
    items_returned = dirs_len + files_len + 1 * (not not root)
    total_stats = {}
    if stat_data:
        if list_stat_data:
            for key in list_stat_data["statistics"]:
                total_stats[key] = list_stat_data["statistics"][key] + stat_data["statistics"][key]
        else:
            total_stats = stat_data.get("statistics", {})
    else:
        total_stats = list_stat_data.get("statistics", {})

    # Build response
    resp_data = {
        "contents": {
            "dirs": list_stat_data.get("dirs", []),
            "files": list_stat_data.get("files", []),
            "root": root,
        },
        "items_total": items_total,
        "items_returned": items_returned,
        "token_continuation": token_continuation,
        "token_expiration": token_expiration,
        "statistics": total_stats,
    }
    if param["type"] == DATA_TYPE_DISKOVER:
        convert_response_to_diskover(resp_data)
    return Response(json.dumps(resp_data, default=lambda o: JSON_SER_ERR), mimetype=MIME_TYPE_JSON)


@app.route("/ps_stat/single", methods=["GET"])
def handle_ps_stat_single():
    """Returns metadata for a single file or directory specified by the "path" argument

    Query arguments (common)
    ----------
    fields: <string> Comma separated string with a complete list of field names to return. Defaults to the empty string
            which returns all fields.
    path: <string> URL encoded string representing the path in the file system that metadata will be returned
            The path can start with /ifs or not. The /ifs part of the path will be prepended if necessary
            A path with a trailing slash will have the slash removed.
    type: <string> Type of scan result to return. One of: powerscale|diskover. The default is powerscale.

    Query arguments (optional)
    ----------
    custom_tagging: <bool> When true call a custom handler for each file. Enabling this can slow down scan speed
    extra_attr: <bool> When true, gets extra OneFS metadata. Enabling this can slow down scan speed
    no_acl: <bool> When true, skip ACL parsing. Enabling this can speed up scanning but results will not have ACLs
    strip_dot_snapshot: <bool> When true, strip the .snapshot name from the file path returned
    user_attr: <bool> # When true, get user attribute data for files. Enabling this can slow down scan speed

    Returns
    ----------
    dict - A dictionary representing the single item scanned
        {
          "contents": {
            "dirs": []                        # Empty list
            "files": []                       # Empty list
            "root": <dict>                    # Metadata for the root path
            "statistics": {
              "lstat_required": <bool>        # Number of times lstat was called vs. internal stat call
              "not_found": <int>              # Number of files that were not found
              "processed": <int>              # Number of files actually processed
              "skipped": <int>                # Number of files skipped
              "time_access_time": <int>       # Seconds spent getting the file access time
              "time_acl": <int>               # Seconds spent getting file ACL
              "time_custom_tagging": <int>    # Seconds spent processing custom tags
              "time_dinode": <int>            # Seconds spent getting OneFS metadata
              "time_extra_attr": <int>        # Seconds spent getting extra OneFS metadata
              "time_lstat": <int>             # Seconds spent in lstat
              "time_scan_dir": <int>          # Seconds spent scanning the entire directory
              "time_user_attr": <int>         # Seconds spent scanning user attributes
            }
          }
          "items_total": <int>                # Total number of items remaining that could be returned
          "items_returned": <int>             # Number of metadata items returned. This number includes the "root"
          "statistics": {
            "lstat_required": <bool>          # Number of times lstat was called vs. internal stat call
            "not_found": <int>                # Number of files that were not found
            "processed": <int>                # Number of files actually processed
            "skipped": <int>                  # Number of files skipped
            "time_access_time": <int>         # Seconds spent getting the file access time
            "time_acl": <int>                 # Seconds spent getting file ACL
            "time_custom_tagging": <int>      # Seconds spent processing custom tags
            "time_dinode": <int>              # Seconds spent getting OneFS metadata
            "time_extra_attr": <int>          # Seconds spent getting extra OneFS metadata
            "time_lstat": <int>               # Seconds spent in lstat
            "time_scan_dir": <int>            # Seconds spent scanning the entire directory
            "time_user_attr": <int>           # Seconds spent scanning user attributes
          }
        }
    """
    args = request.args
    param = {
        "custom_tagging": misc.parse_arg_bool(args, "custom_tagging", False),
        "extra_attr": misc.parse_arg_bool(args, "extra_attr", False),
        "fields": str(args.get("fields", "")),
        "no_acl": misc.parse_arg_bool(args, "no_acl", False),
        "nodepool_translation": app.config["ps_scan"]["nodepool_translation"],
        "path": args.get("path"),
        "strip_do_snapshot": misc.parse_arg_bool(args, "strip_dot_snapshot", True),
        "type": args.get("type", DEFAULT_DATA_TYPE),
        "user_attr": misc.parse_arg_bool(args, "user_attr", False),
    }
    if param["fields"]:
        # Parse fields to return
        fields = param["fields"].split(",")
        param["fields"] = []
        for field in fields:
            if not re.match(r"[^a-zA-Z0-9_\-,.]", field):
                param["fields"].append(field)
        # Ensure we have a minimum set of fields
        if param["fields"]:
            param["fields"] = list(set(param["fields"] + REQUIRED_RETURN_FIELDS))

    # Get the base directory, the last path component, and the full path. e.g. /ifs, foo, /ifs/foo
    base, file, full = misc.get_path_from_urlencoded(param["path"])
    stat_data = file_handler_pscale(base, [file], param)
    if stat_data["dirs"]:
        root = stat_data["dirs"][0]
    elif stat_data["files"]:
        root = stat_data["files"][0]
    else:
        root = {}
    resp_data = {
        "contents": {
            "dirs": [],
            "files": [],
            "root": root,
        },
        "items_total": 1,
        "items_returned": 1 * (not not root),
        "statistics": stat_data["statistics"],
    }
    if param["type"] == DATA_TYPE_DISKOVER:
        convert_response_to_diskover(resp_data)
    return Response(json.dumps(resp_data, default=lambda o: JSON_SER_ERR), mimetype=MIME_TYPE_JSON)


if __name__ == "__main__" or __file__ == None:
    # Setup command line parser and parse agruments
    (parser, options, args) = cli_parser.parse_cli(sys.argv, __version__, __date__)
    if options["debug"]:
        LOG.setLevel(logging.DEBUG)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGUSR1, signal_handler)
    if misc.is_onefs_os():
        # Set resource limits
        old_limit, new_limit = misc.set_resource_limits(options["ulimit_memory"])
        if new_limit:
            LOG.debug({"msg": "VMEM ulimit value set", "new_value": new_limit})
        else:
            LOG.info({"msg": "VMEM ulimit setting failed", "mem_size": options["ulimit_memory"]})

    app.config["cache"] = SimpleCache(options)
    app.config["ps_scan"] = {
        "options": options,
        "nodepool_translation": misc.get_nodepool_translation(),
    }

    if options["user"]:
        try:
            become_user(options["user"])
        except Exception as e:
            LOG.exception(e)
            sys.exit(1)

    svr_addr = "*" if options["addr"] == DEFAULT_SERVER_ADDR else options["addr"]
    server = create_server(
        app,
        host=svr_addr,
        ident="ps_scan_api_server/{ver}".format(ver=__version__),
        port=options["port"],
        threads=options["threads"],
    )
    server.run()

    # Cleanup SimpleCache
    cache = app.config.get("cache")
    if cache:
        del app.config["cache"]
        cache.__del__()
