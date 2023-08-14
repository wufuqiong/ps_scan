#!/usr/bin/env python
# -*- coding: utf8 -*-
"""
PowerScale file scanner
"""
# fmt: off
__title__         = "ps_scan"
__version__       = "0.1.0"
__date__          = "12 August 2023"
__license__       = "MIT"
__author__        = "Andrew Chung <andrew.chung@dell.com>"
__maintainer__    = "Andrew Chung <andrew.chung@dell.com>"
__email__         = "andrew.chung@dell.com"
# fmt: on
import datetime
import json
import logging
import multiprocessing as mp
import optparse
import os
import platform
import queue
import select
import signal
import sys
import time

import elasticsearch_wrapper
import scanit
import user_handlers
import helpers.cli_parser as cli_parser
import helpers.misc as misc
import helpers.sliding_window_stats as sliding_window_stats
import libs.hydra as Hydra
import libs.remote_run as rr
from helpers.constants import *


LOG = logging.getLogger()


CLIENT_STATE_IDLE = "idle"
CLIENT_STATE_STARTING = "starting"
CLIENT_STATE_RUNNING = "running"
CLIENT_STATE_STOPPED = "stopped"
DEFAULT_QUEUE_TIMEOUT = 1
DEFAULT_LOOPBACK_ADDR = "127.0.0.1"
PS_CMD_DUMPSTATE = "dumpstate"
PS_CMD_QUIT = "quit"
PS_CMD_TOGGLEDEBUG = "toggledebug"
MSG_TYPE_CLIENT_DATA = "client_data"
MSG_TYPE_CLIENT_CLOSED = "client_closed"
MSG_TYPE_CLIENT_CONNECT = "client_connect"
MSG_TYPE_CLIENT_DIR_LIST = "client_dir_list"
MSG_TYPE_CLIENT_QUIT = "client_quit"
MSG_TYPE_CLIENT_REQ_DIR_LIST = "client_req_dir_list"
MSG_TYPE_CLIENT_STATE_IDLE = "client_state_idle"
MSG_TYPE_CLIENT_STATE_RUNNING = "client_state_running"
MSG_TYPE_CLIENT_STATE_STOPPED = "client_state_stopped"
MSG_TYPE_CLIENT_STATUS_DIR_COUNT = "client_status_dir_count"
MSG_TYPE_CLIENT_STATUS_STATS = "client_status_stats"
MSG_TYPE_COMMAND = "cmd"
MSG_TYPE_CONFIG_UPDATE = "config_update"
MSG_TYPE_DEBUG = "debug"
MSG_TYPE_QUIT = "quit"
MSG_TYPE_REMOTE_CALLBACK = "remote_callback"


class PSScanClient(object):
    def __init__(self, args={}):
        """Initialize PSScanClient

        Parameters
        ----------
        args: dictionary
            dir_output_interval: int - Time in seconds between each directory queue update to the server
            dir_request_interval: int - Limit how often a client can request more work directories, in seconds
            poll_interval: int - Time in seconds to wait in the select statement
            scanner_file_handler: function pointer -
            scanner_dir_chunk: int -
            scanner_dir_priority_count: int -
            scanner_dir_request_percent: float - Percentage of the queued working directories to return per request
            scanner_file_chunk: int -
            scanner_file_q_cutoff: int -
            scanner_file_q_min_cutoff: int -
            scanner_threads: int - Number of threads the file scanner should use
            server_addr: str - IP/FQDN of the ps_scan server process
            server_port: int - Port to connect to the ps_scan server
            stats_interval: int - Time in seconds between each statistics update to the server
        """
        self.client_config = {}
        self.dir_output_count = 0
        self.dir_output_interval = args.get("dir_output_interval", DEFAULT_DIR_OUTPUT_INTERVAL)
        self.dir_request_interval = args.get("dir_request_interval", DEFAULT_DIR_REQUEST_INTERVAL)
        self.poll_interval = args.get("poll_interval", DEFAULT_CMD_POLL_INTERVAL)
        self.scanner = scanit.ScanIt()
        # TODO: Change how file handler is called
        self.scanner_file_handler = args.get("scanner_file_handler", user_handlers.file_handler_basic)
        self.scanner_dir_chunk = args.get("scanner_dir_chunk", scanit.DEFAULT_QUEUE_DIR_CHUNK_SIZE)
        self.scanner_dir_priority_count = args.get("scanner_dir_priority_count", scanit.DEFAULT_DIR_PRIORITY_COUNT)
        self.scanner_dir_request_percent = args.get("scanner_dir_request_percent", DEFAULT_DIRQ_REQUEST_PERCENTAGE)
        self.scanner_file_chunk = args.get("scanner_file_chunk", scanit.DEFAULT_QUEUE_FILE_CHUNK_SIZE)
        self.scanner_file_q_cutoff = args.get("scanner_file_q_cutoff", scanit.DEFAULT_FILE_QUEUE_CUTOFF)
        self.scanner_file_q_min_cutoff = args.get("scanner_file_q_min_cutoff", scanit.DEFAULT_FILE_QUEUE_MIN_CUTOFF)
        self.scanner_threads = args.get("scanner_threads", DEFAULT_THREAD_COUNT)
        self.sent_data = 0
        self.server_addr = args.get("server_addr", Hydra.DEFAULT_SERVER_ADDR)
        self.server_port = args.get("server_port", Hydra.DEFAULT_SERVER_PORT)
        self.socket = Hydra.HydraSocket(
            {
                "server_addr": self.server_addr,
                "server_port": self.server_port,
            }
        )
        self.stats_output_count = 0
        self.stats_output_interval = args.get("stats_interval", DEFAULT_STATS_OUTPUT_INTERVAL)
        self.status = CLIENT_STATE_STARTING
        self.wait_list = [self.socket]
        self.want_data = time.time()
        self.work_list_count = 0
        self._init_scanner()

    def _exec_send_dir_list(self, dir_list):
        self.socket.send(
            {
                "type": MSG_TYPE_CLIENT_DIR_LIST,
                "work_item": dir_list,
            }
        )

    def _exec_send_req_dir_list(self):
        self.socket.send(
            {
                "type": MSG_TYPE_CLIENT_REQ_DIR_LIST,
            }
        )

    def _exec_send_client_state_idle(self):
        self.socket.send(
            {
                "type": MSG_TYPE_CLIENT_STATE_IDLE,
            }
        )

    def _exec_send_client_state_running(self):
        self.socket.send(
            {
                "type": MSG_TYPE_CLIENT_STATE_RUNNING,
            }
        )

    def _exec_send_status_stats(self, now):
        stats_data = self.stats_merge(now)
        self.socket.send(
            {
                "type": MSG_TYPE_CLIENT_STATUS_STATS,
                "data": stats_data,
            }
        )
        LOG.debug("DEBUG: LOCAL STATS: {stats}".format(stats=stats_data))

    def _exec_send_status_dir_count(self):
        self.socket.send(
            {
                "type": MSG_TYPE_CLIENT_STATUS_DIR_COUNT,
                "data": self.work_list_count,
            }
        )

    def _init_scanner(self):
        s = self.scanner
        for attrib in [
            "dir_chunk",
            "dir_priority_count",
            "file_chunk",
            "file_q_cutoff",
            "file_q_min_cutoff",
        ]:
            setattr(s, attrib, getattr(self, "scanner_" + attrib))
        s.exit_on_idle = False
        s.num_threads = self.scanner_threads
        s.processing_type = scanit.PROCESS_TYPE_ADVANCED
        # TODO: Change how custom states, init and file handler work
        s.handler_custom_stats = user_handlers.custom_stats_handler
        s.handler_init_thread = user_handlers.init_thread
        s.handler_file = self.scanner_file_handler
        # TODO: Change how the user handler is passed in and initialized
        custom_state, custom_threads_state = s.get_custom_state()
        user_handlers.init_custom_state(custom_state)

    def connect(self):
        LOG.info("Connecting to server at {svr}:{port}".format(svr=self.server_addr, port=self.server_port))
        connected = self.socket.connect()
        if not connected:
            LOG.info("Unable to connect to server")
            return
        continue_running = True
        start_wall = time.time()
        self.scanner.start()
        # Send initial empty stats block to the server
        self._exec_send_status_stats(time.time())

        # Main client processing loop
        while continue_running:
            rlist, _, xlist = select.select(self.wait_list, [], self.wait_list, self.poll_interval)
            now = time.time()
            self.work_list_count = self.scanner.get_dir_queue_size()
            if rlist:
                data = self.socket.recv()
                msg_type = data.get("type")
                if msg_type == MSG_TYPE_COMMAND:
                    cmd = data.get("cmd")
                    LOG.debug("Command received: {cmd}".format(cmd=cmd))
                    if cmd == "closed":
                        self.disconnect()
                        continue_running = False
                        continue
                elif msg_type == "data":
                    msg_data = data.get("data")
                    response = self.parse_message(msg_data, now)
                    if response.get("cmd") == PS_CMD_QUIT:
                        self.disconnect()
                        continue_running = False
                        continue
                elif msg_type is None:
                    LOG.debug("Socket ready to read but no data was received. We should shutdown now.")
                    self.disconnect()
                    continue_running = False
                    continue
                else:
                    LOG.debug("Unexpected message received: {data}".format(data=data))
            if xlist:
                LOG.error("Socket encountered an error or was closed")
                self.disconnect()
                continue_running = False
                break

            # Determine if we should send a statistics update
            cur_stats_count = (now - start_wall) // self.stats_output_interval
            if cur_stats_count > self.stats_output_count:
                self.stats_output_count = cur_stats_count
                self._exec_send_status_stats(now)

            # Determine if we should send a directory queue count update
            cur_dir_count = (now - start_wall) // self.dir_output_interval
            if cur_dir_count > self.dir_output_count:
                self.dir_output_count = cur_dir_count
                self._exec_send_status_dir_count()

            # Ask parent process for more data if required, limit data requests to dir_request_interval seconds
            if (self.work_list_count == 0) and (now - self.want_data > self.dir_request_interval):
                self.want_data = now
                self._exec_send_req_dir_list()

            # Check if the scanner is idle
            if (
                not self.work_list_count
                and not self.scanner.get_file_queue_size()
                and not self.scanner.is_processing()
                and self.status != CLIENT_STATE_IDLE
            ):
                self.status = CLIENT_STATE_IDLE
                self._exec_send_client_state_idle()
                # Send a stats update whenever we go idle
                self._exec_send_status_stats(now)

    def disconnect(self):
        custom_state, custom_threads_state = self.scanner.get_custom_state()
        user_handlers.shutdown(custom_state, custom_threads_state)
        if self.scanner:
            self.scanner.terminate()
            self.scanner = None
        if self.socket in self.wait_list:
            self.wait_list.remove(self.socket)
        if self.socket:
            self.socket.disconnect()
            self.socket = None

    def dump_state(self):
        LOG.critical("\nDumping state\n" + "=" * 20)
        state = {}
        for member in [
            "client_config",
            "dir_output_count",
            "dir_output_interval",
            "dir_request_interval",
            "poll_interval",
            "scanner_dir_chunk",
            "scanner_dir_priority_count",
            "scanner_dir_request_percent",
            "scanner_file_chunk",
            "scanner_file_q_cutoff",
            "scanner_file_q_min_cutoff",
            "scanner_threads",
            "sent_data",
            "server_addr",
            "server_port",
            "stats_output_count",
            "stats_output_interval",
            "status",
            "wait_list",
            "want_data",
            "work_list_count",
        ]:
            state[member] = str(getattr(self, member))
        state["dir_q_size"] = self.scanner.get_dir_queue_size()
        state["file_q_size"] = self.scanner.get_file_queue_size()
        LOG.critical(json.dumps(state, indent=2, sort_keys=True))

    def parse_config_update(self, cfg):
        # TODO: Re-architect how config updates are sent to the user handler/plug-in
        custom_state, custom_threads_state = self.scanner.get_custom_state()
        user_handlers.update_config(custom_state, cfg)
        self.client_config = cfg

    def parse_config_update_log_level(self, cfg):
        log_level = cfg.get("log_level")
        if not log_level:
            LOG.error("log_level missing from cfg while updating the log level.")
            return
        LOG.setLevel(log_level)

    def parse_config_update_logger(self, cfg):
        format_string_vars = {
            "filename": platform.node(),
            "pid": os.getpid(),
        }
        try:
            logger_block = cfg.get("logger")
            if logger_block["destination"] == "file":
                log_filename = logger_block["filename"].format(**format_string_vars)
                log_handler = logging.FileHandler(log_filename)
                log_handler.setFormatter(logging.Formatter(logger_block["format"]))
                LOG.handlers[:] = []
                LOG.addHandler(log_handler)
                LOG.setLevel(logger_block["level"])
        except KeyError as ke:
            sys.stderr.write("ERROR: Logger filename string is invalid: {txt}\n".format(txt=str(ke)))
        except Exception as e:
            sys.stderr.write("ERROR: Unhandled exception while trying to configure logger: {txt}\n".format(txt=str(ke)))

    def parse_message(self, msg, now):
        msg_type = msg.get("type")
        if msg_type == MSG_TYPE_CLIENT_DIR_LIST:
            work_items = msg.get("work_item")
            if not work_items:
                return
            self.scanner.add_scan_path(work_items)
            self.status = CLIENT_STATE_RUNNING
            self.want_data = 0
            self.work_list_count = self.scanner.get_dir_queue_size()
            self._exec_send_client_state_running()
            LOG.debug(
                "{cmd}: Received {count} work items to process".format(
                    cmd=msg_type,
                    count=len(work_items),
                )
            )
        elif msg_type == MSG_TYPE_CLIENT_QUIT:
            return {"cmd": PS_CMD_QUIT}
        elif msg_type == MSG_TYPE_CLIENT_REQ_DIR_LIST:
            dir_list = self.scanner.get_dir_queue_items(
                num_items=1, percentage=msg.get("pct", self.scanner_dir_request_percent)
            )
            if dir_list:
                self._exec_send_dir_list(dir_list)
            LOG.debug(
                "{cmd}: Asked to return work items. Returning {count} items.".format(
                    cmd=msg_type,
                    count=len(dir_list),
                )
            )
        elif msg_type == MSG_TYPE_CONFIG_UPDATE:
            cfg = msg.get("config")
            if "logger" in cfg:
                self.parse_config_update_logger(cfg)
            if "log_level" in cfg:
                self.parse_config_update_log_level(cfg)
            if "client_config" in cfg:
                self.parse_config_update(cfg)
        elif msg_type == MSG_TYPE_DEBUG:
            dbg = msg.get("cmd")
            if "dump_state" in dbg:
                self.dump_state()
        else:
            LOG.debug("Unhandled message: {msg}".format(msg=msg))
        return {}

    def stats_merge(self, now):
        return self.scanner.get_stats()


class PSScanCommandClient(object):
    def __init__(self, args={}):
        self.commands = args.get("commands", [])
        self.server_addr = args.get("server_addr", Hydra.DEFAULT_SERVER_ADDR)
        self.server_port = args.get("server_port", Hydra.DEFAULT_SERVER_PORT)
        self.socket = Hydra.HydraSocket(
            {
                "server_addr": self.server_addr,
                "server_port": self.server_port,
            }
        )

    def send_command(self):
        if not self.commands:
            LOG.info("No commands to send. No connection to server required.")
            return
        LOG.info("Connecting to server at {svr}:{port}".format(svr=self.server_addr, port=self.server_port))
        connected = self.socket.connect()
        if not connected:
            LOG.info("Unable to connect to server")
            return
        cmd = self.commands[0]
        LOG.info('Sending "{cmd}" command to server'.format(cmd=cmd))
        if cmd in (PS_CMD_DUMPSTATE, PS_CMD_TOGGLEDEBUG, PS_CMD_QUIT):
            self.socket.send({"type": MSG_TYPE_COMMAND, "cmd": cmd})
        else:
            LOG.error("Unknown command: {cmd}".format(cmd=cmd))
        time.sleep(1)
        self.socket.disconnect()


class PSScanServer(Hydra.HydraServer):
    def __init__(self, args={}):
        """Initialize PSScanClient

        Parameters
        ----------
        args: dictionary
            node_list: list - List of clients to auto-start. Format of each entry is in remote_run module
            queue_timeout: int - Number of seconds to wait for new messages before continuing with the processing loop
            request_work_interval: int - Number of seconds between requests to a client to return work
            scan_path: list - List of paths to scan
            script_path: str - Full path to script to run on clients
            server_connect_addr:str - FQDN/IP that clients should use to connect
            stats_interval: int - Number of seconds between each statistics update
        """
        args["async_server"] = True
        super(PSScanServer, self).__init__(args=args)
        self.client_config = args.get("client_config", {})
        self.client_count = 0
        self.client_state = {}
        self.connect_addr = args.get("server_connect_addr", None)
        self.msg_q = queue.Queue()
        self.node_list = args.get("node_list", None)
        self.queue_timeout = args.get("queue_timeout", DEFAULT_QUEUE_TIMEOUT)
        self.remote_state = None
        self.request_work_interval = args.get("request_work_interval", DEFAULT_REQUEST_WORK_INTERVAL)
        self.request_work_percentage = args.get("request_work_percentage", DEFAULT_DIRQ_REQUEST_PERCENTAGE)
        self.script_path = args.get("script_path", None)
        self.stats_fps_window = sliding_window_stats.SlidingWindowStats(STATS_FPS_BUCKETS)
        self.stats_last_files_processed = 0
        self.stats_output_count = 0
        self.stats_output_interval = args.get("stats_interval", DEFAULT_STATS_OUTPUT_INTERVAL)
        self.work_list = args.get("scan_path", [])
        if not (self.connect_addr and self.script_path):
            raise Exception("Server connect address and script path is required")
        # Setup signal handlers
        signal.signal(signal.SIGINT, self.handler_signal_interrupt)
        signal.signal(signal.SIGUSR1, self.handler_signal_usr1)
        signal.signal(signal.SIGUSR2, self.handler_signal_usr2)

    def _exec_dump_state(self):
        msg = {
            "type": MSG_TYPE_DEBUG,
            "cmd": {
                "dump_state": True,
            },
        }
        self.send_all_clients(msg)
        self.dump_state()

    def _exec_send_config_update(self, client):
        self.send(
            client,
            {
                "type": MSG_TYPE_CONFIG_UPDATE,
                "config": {
                    "client_config": self.client_config,
                    "logger": {
                        "format": "%(asctime)s - %(levelname)s - [%(module)s:%(lineno)d] - (%(process)d|%(threadName)s) %(message)s",
                        "destination": "file",
                        "filename": "log-{filename}-{pid}.txt",
                        "level": LOG.getEffectiveLevel(),
                    },
                },
            },
        )

    def _exec_send_one_work_item(self, client):
        if not self.work_list:
            return False
        try:
            work_item = self.work_list.pop(0)
            self.send(
                client,
                {
                    "type": MSG_TYPE_CLIENT_DIR_LIST,
                    "work_item": [work_item],
                },
            )
            return True
        except queue.Empty as qe:
            LOG.error("Work queue was not empty but unable to get a work item to send to the new client")
        return False

    def _exec_send_quit(self, client):
        self.send(
            client,
            {
                "type": MSG_TYPE_CLIENT_QUIT,
            },
        )

    def _exec_send_req_dir_list(self, client):
        self.send(
            client,
            {
                "type": MSG_TYPE_CLIENT_REQ_DIR_LIST,
                "pct": self.request_work_percentage,
            },
        )

    def _exec_send_work_items(self, client, work_items):
        self.send(
            client,
            {
                "type": MSG_TYPE_CLIENT_DIR_LIST,
                "work_item": work_items,
            },
        )
        return True

    def _exec_toggle_debug(self):
        cur_level = LOG.getEffectiveLevel()
        if cur_level != logging.DEBUG:
            LOG.setLevel(logging.DEBUG)
        else:
            LOG.setLevel(logging.INFO)
        # Send a log level update to all clients
        msg = {
            "type": MSG_TYPE_CONFIG_UPDATE,
            "config": {
                "log_level": LOG.getEffectiveLevel(),
            },
        }
        self.send_all_clients(msg)

    def dump_state(self):
        LOG.critical("\nDumping state\n" + "=" * 20)
        state = {}
        for member in [
            "client_count",
            "client_state",
            "connect_addr",
            "node_list",
            "queue_timeout",
            "remote_state",
            "request_work_interval",
            "request_work_percentage",
            "script_path",
            "stats_last_files_processed",
            "stats_output_count",
            "stats_output_interval",
            "work_list",
        ]:
            state[member] = str(getattr(self, member))
        state["stats_fps_window"] = self.stats_fps_window.get_all_windows()
        LOG.critical(json.dumps(state, indent=2, sort_keys=True))

    def handler_client_command(self, client, msg):
        """
        Users should override this method to add their own handler for client commands.
        """
        if not msg or (msg["type"] == MSG_TYPE_COMMAND and msg["cmd"] == "closed"):
            self.msg_q.put({"type": MSG_TYPE_CLIENT_CLOSED, "data": msg, "client": client})
            self._remove_client(client)
        elif msg["type"] == "data":
            self.msg_q.put({"type": MSG_TYPE_CLIENT_DATA, "data": msg["data"], "client": client})

    def handler_client_connect(self, client):
        self.msg_q.put({"type": MSG_TYPE_CLIENT_CONNECT, "client": client})

    def handler_signal_interrupt(self, signum, frame):
        LOG.debug("SIGINT signal received. Quiting program.")
        self.msg_q.put({"type": MSG_TYPE_QUIT})

    def handler_signal_usr1(self, signum, frame):
        LOG.debug("SIGUSR1 signal received. Toggling debug.")
        self._exec_toggle_debug()

    def handler_signal_usr2(self, signum, frame):
        LOG.debug("SIGUSR2 signal received. Dumping state.")
        self._exec_dump_state()

    def launch_remote_processes(self):
        run_cmd = [
            "python",
            self.script_path,
            "--op",
            "client",
            "--port",
            str(self.server_port),
            "--addr",
            self.connect_addr,
        ]
        if self.node_list:
            for node in self.node_list:
                if node.get("type") != "default":
                    node["cmd"] = run_cmd
            LOG.debug("Launching remote process with cmd: {cmd}".format(cmd=run_cmd))
            self.remote_state = rr.RemoteRun({"callback": self.remote_callback})
            self.remote_state.connect(self.node_list)
        LOG.debug("All remote processes launched.")

    def output_statistics(self, now, start_wall):
        temp_stats = misc.merge_process_stats(self.client_state) or {}
        new_files_processed = temp_stats.get("files_processed", self.stats_last_files_processed)
        self.stats_fps_window.add_sample(new_files_processed - self.stats_last_files_processed)
        self.stats_last_files_processed = new_files_processed
        self.print_interim_statistics(
            temp_stats,
            now,
            start_wall,
            self.stats_fps_window,
            self.stats_output_interval,
        )

    def output_statistics_final(self, now, total_time):
        temp_stats = misc.merge_process_stats(self.client_state) or {}
        self.print_final_statistics(temp_stats, self.client_count, total_time)

    def parse_message(self, msg, now):
        if msg["type"] == MSG_TYPE_CLIENT_DATA:
            client_idx = msg["client"]
            cur_client = self.client_state.get(client_idx)
            data = msg["data"]
            cid = cur_client["id"]
            data_type = data.get("type")
            if data_type == "cmd":
                cmd = data.get("cmd")
                LOG.debug("[client:{cid}] - Command: {cmd}".format(cid=cid, cmd=cmd))
                if cmd == PS_CMD_QUIT:
                    return {"cmd": PS_CMD_QUIT}
                elif cmd == PS_CMD_DUMPSTATE:
                    self._exec_dump_state()
                elif cmd == PS_CMD_TOGGLEDEBUG:
                    self._exec_toggle_debug()
                else:
                    LOG.error("[client:{cid}] - Unknown command: {cmd}".format(cid=cid, cmd=cmd))
            elif data_type == MSG_TYPE_CLIENT_DIR_LIST:
                LOG.debug("[client:{cid}] - returned_directories:{data}".format(cid=cid, data=len(data["work_item"])))
                cur_client["sent_data"] = 0
                cur_client["want_data"] = 0
                # Extend directory work list with items returned by the client
                self.work_list.extend(data["work_item"])
            elif data_type == MSG_TYPE_CLIENT_STATE_IDLE:
                LOG.debug("[client:{cid}] - New state: {data}".format(cid=cid, data="IDLE"))
                cur_client["status"] = CLIENT_STATE_IDLE
                cur_client["want_data"] = now
            elif data_type == MSG_TYPE_CLIENT_STATE_RUNNING:
                LOG.debug("[client:{cid}] - New state: {data}".format(cid=cid, data="RUNNING"))
                cur_client["status"] = CLIENT_STATE_RUNNING
                cur_client["want_data"] = 0
            elif data_type == MSG_TYPE_CLIENT_STATE_STOPPED:
                LOG.debug("[client:{cid}] - New state: {data}".format(cid=cid, data="STOPPED"))
                cur_client["status"] = CLIENT_STATE_STOPPED
                cur_client["want_data"] = 0
            elif data_type == MSG_TYPE_CLIENT_STATUS_DIR_COUNT:
                LOG.debug("[client:{cid}] - has_queued_directories:{data}".format(cid=cid, data=data["data"]))
                cur_client["dir_count"] = data["data"]
            elif data_type == MSG_TYPE_CLIENT_STATUS_STATS:
                LOG.debug("[client:{cid}] - Sent statistics".format(cid=cid))
                cur_client["stats"] = data["data"]
                cur_client["stats_time"] = now
            elif data_type == MSG_TYPE_CLIENT_REQ_DIR_LIST:
                LOG.debug("[client:{cid}] - Requested directory list".format(cid=cid))
                cur_client["want_data"] = now
            else:
                LOG.error("[client:{cid}] - Unknown command: {cmd}".format(cid=cid, cmd=data_type))
        elif msg["type"] == MSG_TYPE_CLIENT_CLOSED:
            cur_client = self.client_state.get(msg["client"])
            LOG.debug("[client:{cid}] - Socket closed: {data}".format(cid=cur_client["id"], data=msg))
            cur_client["dir_count"] = 0
            cur_client["sent_data"] = 0
            cur_client["status"] = CLIENT_STATE_STOPPED
            cur_client["want_data"] = 0
        elif msg["type"] == MSG_TYPE_CLIENT_CONNECT:
            client_idx = msg["client"]
            self.client_count += 1
            LOG.debug("[client:{cid}] - Socket connected: {data}".format(cid=self.client_count, data=msg))
            state_obj = {
                "client": client_idx,
                "dir_count": 0,
                "id": self.client_count,
                "sent_data": 0,
                "stats": {},
                "stats_time": None,
                "status": CLIENT_STATE_STARTING,
                "want_data": now,
            }
            # Send configuration to client
            self._exec_send_config_update(client_idx)
            # Send up to 1 directory in our work queue to each connected client
            work_sent = self._exec_send_one_work_item(client_idx)
            if work_sent:
                state_obj["want_data"] = 0
            self.client_state[client_idx] = state_obj
        elif msg["type"] == MSG_TYPE_QUIT:
            LOG.debug("Received internal quit command")
            return {"cmd": PS_CMD_QUIT}
        elif msg["type"] == MSG_TYPE_REMOTE_CALLBACK:
            # This type of command is sent from the remote_run module that handles spawning processes on other machines
            # TODO: Add code to handle re-launching dead processes
            # TODO: Log any console output if there is an error
            LOG.debug("Remote process message from client {client}: {data}".format(client=msg["client"], data=msg))
        else:
            LOG.debug("Unhandled message received: {data}".format(data=msg))
        return {}

    def print_interim_statistics(self, stats, now, start, fps_window, interval):
        buckets = [str(x) for x in fps_window.get_window_sizes()]
        fps_per_bucket = ["{fps:,.1f}".format(fps=x / interval) for x in fps_window.get_all_windows()]
        sys.stdout.write(
            """{ts} - Statistics:
        Current run time (s): {runtime:,d}
        FPS overall / recent ({fps_buckets}) intervals: {fps:,.1f} / {fps_per_bucket}
        Total file bytes processed: {f_bytes:,d}
        Files (Processed/Queued/Skipped): {f_proc:,d} / {f_queued:,d} / {f_skip:,d}
        File Q Size/Handler time: {f_q_size:,d} / {f_h_time:,.1f}
        Dir scan time: {d_scan:,.1f}
        Dirs (Processed/Queued/Skipped): {d_proc:,d} / {d_queued:,d} / {d_skip:,d}
        Dir Q Size/Handler time: {d_q_size:,d} / {d_h_time:,.1f}
""".format(
                d_proc=stats.get("dirs_processed", 0),
                d_h_time=stats.get("dir_handler_time", 0),
                d_q_size=stats.get("dir_q_size", 0),
                d_queued=stats.get("dirs_queued", 0),
                d_scan=stats.get("dir_scan_time", 0),
                d_skip=stats.get("dirs_skipped", 0),
                f_bytes=stats.get("file_size_total", 0),
                f_h_time=stats.get("file_handler_time", 0),
                f_proc=stats.get("files_processed", 0),
                f_q_size=stats.get("file_q_size", 0),
                f_queued=stats.get("files_queued", 0),
                f_skip=stats.get("files_skipped", 0),
                fps=stats.get("files_processed", 0) / (now - start),
                fps_buckets=", ".join(buckets),
                fps_per_bucket=" - ".join(fps_per_bucket),
                runtime=int(now - start),
                ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

    def print_final_statistics(self, stats, num_clients, wall_time):
        sys.stdout.write(
            """Final statistics
        Wall time (s): {wall_tm:,.2f}
        Average Q wait time (s): {avg_q_tm:,.2f}
        Total time spent in dir/file handler routines across all clients (s): {dht:,.2f} / {fht:,.2f}
        Processed/Queued/Skipped dirs: {p_dirs:,d} / {q_dirs:,d} / {s_dirs:,d}
        Processed/Queued/Skipped files: {p_files:,d} / {q_files:,d} / {s_files:,d}
        Total file size: {fsize:,d}
        Avg files/second: {a_fps:,.1f}
""".format(
                wall_tm=wall_time,
                avg_q_tm=stats.get("q_wait_time", 0) / num_clients,
                dht=stats.get("dir_handler_time", 0),
                fht=stats.get("file_handler_time", 0),
                p_dirs=stats.get("dirs_processed", 0),
                q_dirs=stats.get("dirs_queued", 0),
                s_dirs=stats.get("dirs_skipped", 0),
                p_files=stats.get("files_processed", 0),
                q_files=stats.get("files_queued", 0),
                s_files=stats.get("files_skipped", 0),
                fsize=stats.get("file_size_total", 0),
                a_fps=(stats.get("files_processed", 0) + stats.get("files_skipped", 0)) / wall_time,
            ),
        )

    def remote_callback(self, client, client_id, msg=None):
        self.msg_q.put({"type": MSG_TYPE_REMOTE_CALLBACK, "data": msg, "client_id": client_id, "client": client})

    def serve(self):
        LOG.info("Starting server")
        start_wall = time.time()
        self.start()
        self.launch_remote_processes()

        # Start main processing loop
        # Wait for clients to connect, request work, and redistribute work as needed.
        continue_running = True
        while continue_running:
            now = time.time()
            try:
                queue_item = self.msg_q.get(timeout=self.queue_timeout)
            except queue.Empty as qe:
                queue_item = None
            except Exception as e:
                LOG.exception(e)
                continue_running = False
                continue
            else:
                try:
                    response = self.parse_message(queue_item, now)
                    if response.get("cmd") == PS_CMD_QUIT:
                        continue_running = False
                        continue
                except Exception as e:
                    # parse_message should handle exceptions. Any uncaught exceptions should terminate the program.
                    LOG.exception(e)
                    continue_running = False
                    continue

            try:
                # Output statistics
                #   The -1 is for a 1 second offset to allow time for stats to come from processes
                cur_stats_count = (now - start_wall) // self.stats_output_interval
                if cur_stats_count > self.stats_output_count:
                    self.stats_output_count = cur_stats_count
                    self.output_statistics(now, start_wall)

                # Check all our client states to gather which are idle, which have work dirs, and which want work
                continue_running = False
                idle_clients = 0
                have_dirs_clients = []
                want_work_clients = []
                client_keys = self.client_state.keys()
                for key in client_keys:
                    client = self.client_state[key]
                    if not continue_running and client["status"] != CLIENT_STATE_STOPPED:
                        continue_running = True
                    if client["status"] in (CLIENT_STATE_IDLE, CLIENT_STATE_STOPPED):
                        idle_clients += 1
                    # Check if we need to request or send any directories to existing processes
                    if client["want_data"]:
                        want_work_clients.append(key)
                    # Any processes that have directories are checked
                    if client["dir_count"] > 1:
                        have_dirs_clients.append(key)
                if not continue_running and self.work_list:
                    # If there are no connected clients and there is work to do then continue running
                    continue_running = True

                # If all sub-processes are idle and we have no work items, we can terminate all the scanner processes
                if idle_clients == len(client_keys) and not self.work_list:
                    for key in client_keys:
                        client = self.client_state[key]
                        if client["status"] != CLIENT_STATE_STOPPED:
                            self._exec_send_quit(key)
                    # Skip any further processing and just wait for processes to end
                    continue

                # Send out our directories to all processes that want work if we have work to send
                if want_work_clients and self.work_list:
                    LOG.debug("DEBUG: Server has work and has clients that want work")
                    got_work_clients = []
                    len_dir_list = len(self.work_list)
                    len_want_work_procs = len(want_work_clients)
                    increment = (len_dir_list // len_want_work_procs) + (1 * (len_dir_list % len_want_work_procs != 0))
                    index = 0
                    for client_key in want_work_clients:
                        work_dirs = self.work_list[index : index + increment]
                        if not work_dirs:
                            continue
                        self._exec_send_work_items(client_key, work_dirs)
                        self.client_state[client_key]["want_data"] = 0
                        index += increment
                        got_work_clients.append(client_key)
                    # Remove from the want_work_clients list, any clients that got work sent to it
                    for client_key in got_work_clients:
                        want_work_clients.remove(client_key)
                    # Clear the dir_list variable now since we have sent all our work out
                    self.work_list[:] = []

                # If processes want work and we know some processes have work, request those processes return work
                if want_work_clients and have_dirs_clients:
                    LOG.debug("DEBUG: WANT WORK PROCS & HAVE DIR PROCS")
                    for client_key in have_dirs_clients:
                        client = self.client_state[client_key]
                        LOG.debug("DEBUG: CLIENT: %s has dirs, evaluating if we should send message" % client_key)
                        # Limit the number of times we request data from each client to request_work_interval seconds
                        if (now - client["sent_data"]) > self.request_work_interval:
                            LOG.debug("DEBUG: ACTUALLY SENDING CMD_REQ_DIR to client: %s" % client_key)
                            self._exec_send_req_dir_list(client_key)
                            client["sent_data"] = now
            except Exception as e:
                LOG.exception("Exception while in server loop")
                continue_running = False
        total_wall_time = time.time() - start_wall
        self.output_statistics_final(now, total_wall_time)
        LOG.info("{prog} shutting down.".format(prog=__title__))
        self.shutdown()
        LOG.info("{prog} shutdown complete.".format(prog=__title__))

    def shutdown(self):
        super(PSScanServer, self).shutdown()
        self.remote_state.terminate()


def get_script_path():
    return os.path.abspath(__file__)


def read_es_cred_file(filename):
    es_creds = {}
    try:
        with open(filename) as f:
            lines = f.readlines()
            es_creds["user"] = lines[0].strip()
            es_creds["password"] = lines[1].strip()
            if len(lines) > 2:
                es_creds["index"] = lines[2].strip()
            if len(lines) > 3:
                es_creds["url"] = lines[3].strip()
    except:
        LOG.critical("Unable to open or read the credentials file: {file}".format(file=filename))
        sys.exit(3)
    return es_creds


def main():
    # Setup command line parser and parse agruments
    (parser, options, args) = cli_parser.parse_cli(sys.argv, __version__, __date__)

    # Validate command line options
    cmd_line_errors = []
    if len(args) == 0 and options["op"] in (DEFAULT_OPERATION_TYPE_AUTO, DEFAULT_OPERATION_TYPE_SERVER):
        cmd_line_errors.append("***** A minimum of 1 path to scan is required to be specified on the command line.")
    if cmd_line_errors:
        parser.print_help()
        sys.stderr.write("\n" + "\n".join(cmd_line_errors) + "\n")
        sys.exit(1)

    es_credentials = {}
    if options["es_cred_file"]:
        es_credentials = read_es_cred_file(options["es_cred_file"])
    elif options["es_index"] and options["es_user"] and options["es_pass"] and options["es_url"]:
        es_credentials = {
            "index": options["es_index"],
            "password": options["es_pass"],
            "url": options["es_url"],
            "user": options["es_user"],
        }

    if options["type"] == DEFAULT_SCAN_TYPE_AUTO:
        if misc.is_onefs_os():
            options["type"] = DEFAULT_SCAN_TYPE_ONEFS
        else:
            options["type"] = DEFAULT_SCAN_TYPE_BASIC
    if options["type"] == DEFAULT_SCAN_TYPE_ONEFS:
        if not misc.is_onefs_os():
            sys.stderr.write(
                "Script is not running on a OneFS operation system. Invalid --type option, use 'basic' instead.\n"
            )
            sys.exit(2)
        # Set resource limits
        old_limit, new_limit = misc.set_resource_limits(options["ulimit_memory"])
        if new_limit:
            LOG.debug("VMEM ulimit value set to: {val}".format(val=new_limit))
        else:
            LOG.info("VMEM ulimit setting failed.")
        file_handler = user_handlers.file_handler_pscale
    else:
        file_handler = user_handlers.file_handler_basic
    LOG.debug("Parsed options:\n{opt}".format(opt=json.dumps(options, indent=2, sort_keys=True)))

    if options["op"] == DEFAULT_OPERATION_TYPE_CLIENT:
        LOG.info("Starting client")
        client = PSScanClient(
            {
                "server_port": options["port"],
                "server_addr": options["addr"],
                "scanner_file_handler": user_handlers.file_handler_pscale,
            }
        )
        try:
            client.connect()
        except Exception as e:
            LOG.exception("Unhandled exception in client.")
    elif options["op"] == "command":
        LOG.info("Sending command to server")
        client = PSScanCommandClient(
            {
                "commands": options["cmd"],
                "server_port": options["port"],
                "server_addr": options["addr"],
            }
        )
        try:
            client.send_command()
        except Exception as e:
            LOG.exception("Unhandled exception while sending command to server.")
    elif options["op"] in (DEFAULT_OPERATION_TYPE_AUTO, DEFAULT_OPERATION_TYPE_SERVER):
        node_list = [
            {
                "endpoint": "5",
                "type": "onefs",
            },
            {
                "endpoint": "7",
                "type": "onefs",
            },
        ]
        """
            {
                "endpoint": "5",
                "type": "onefs",
            },
            {
                "endpoint": "6",
                "type": "onefs",
            },
            {
                "endpoint": "6",
                "type": "onefs",
            },
            {
                "endpoint": "7",
                "type": "onefs",
            },

        """
        ps_scan_server_options = {
            "cli_options": options,
            "client_config": {},
            "scan_path": args,
            "script_path": get_script_path(),
            "server_port": options["port"],
            "server_addr": options["addr"],
            "server_connect_addr": misc.get_local_internal_addr() or DEFAULT_LOOPBACK_ADDR,
            "node_list": None,
        }
        if es_credentials:
            ps_scan_server_options["client_config"]["es_credentials"] = es_credentials
            ps_scan_server_options["client_config"]["es_send_threads"] = options["es_threads"]
        if options["op"] == "auto" and node_list:
            # Setting the node list will cause the server to automatically launch clients
            ps_scan_server_options["node_list"] = node_list

        try:
            es_client = None
            if es_credentials:
                es_client = elasticsearch_wrapper.es_create_connection(
                    es_credentials["url"],
                    es_credentials["user"],
                    es_credentials["password"],
                    es_credentials["index"],
                )
            if es_client and (options["es_init_index"] or options["es_reset_index"]):
                if options["es_reset_index"]:
                    elasticsearch_wrapper.es_delete_index(es_client)
                LOG.debug("Initializing indices for Elasticsearch: {index}".format(index=es_credentials["index"]))
                es_index_settings = elasticsearch_wrapper.es_create_index_settings(
                    {
                        "number_of_shards": options["es_shards"],
                        "number_of_replicas": options["es_replicas"],
                    }
                )
                elasticsearch_wrapper.es_init_index(es_client, es_credentials["index"], es_index_settings)
            if es_client:
                elasticsearch_wrapper.es_start_processing(es_client, {})
            svr = PSScanServer(ps_scan_server_options)
            svr.serve()
            if es_client:
                elasticsearch_wrapper.es_stop_processing(es_client, {})
        except Exception as e:
            LOG.exception("Unhandled exception in server.")


if __name__ == "__main__" or __file__ == None:
    DEFAULT_LOG_FORMAT = (
        "%(asctime)s - %(levelname)s - [%(module)s:%(lineno)d] - (%(process)d|%(threadName)s) %(message)s"
    )
    log_handler = logging.StreamHandler()
    log_handler.setFormatter(logging.Formatter(DEFAULT_LOG_FORMAT))
    LOG.addHandler(log_handler)
    LOG.setLevel(logging.DEBUG)
    # Disable loggers for sub modules
    for mod_name in ["libs.hydra"]:
        module_logger = logging.getLogger(mod_name)
        module_logger.setLevel(logging.WARN)
    # Support scripts built into executable on Windows
    try:
        mp.freeze_support()
        if hasattr(mp, "set_start_method"):
            # Force all OS to behave the same when spawning new process
            mp.set_start_method("spawn")
    except:
        # Ignore these errors as they are either already set or do not apply to this system
        pass
    main()
