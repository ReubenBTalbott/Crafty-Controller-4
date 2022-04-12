import os
import re
import time
import datetime
import base64
import threading
import logging.config
import subprocess
import html
import tempfile
import psutil

# TZLocal is set as a hidden import on win pipeline
from tzlocal import get_localzone
from apscheduler.schedulers.background import BackgroundScheduler

from app.classes.minecraft.stats import Stats
from app.classes.minecraft.mc_ping import ping, ping_bedrock
from app.classes.models.servers import Server_Stats, helper_servers
from app.classes.models.management import helpers_management
from app.classes.models.users import helper_users
from app.classes.models.server_permissions import Permissions_Servers
from app.classes.shared.console import Console
from app.classes.shared.helpers import Helpers
from app.classes.shared.file_helpers import FileHelpers

logger = logging.getLogger(__name__)


class ServerOutBuf:
    lines = {}

    def __init__(self, helper, proc, server_id):
        self.helper = helper
        self.proc = proc
        self.server_id = str(server_id)
        # Buffers text for virtual_terminal_lines config number of lines
        self.max_lines = self.helper.get_setting("virtual_terminal_lines")
        self.line_buffer = ""
        ServerOutBuf.lines[self.server_id] = []
        self.lsi = 0

    def process_byte(self, char):
        if char == os.linesep[self.lsi]:
            self.lsi += 1
        else:
            self.lsi = 0
            self.line_buffer += char

        if self.lsi >= len(os.linesep):
            self.lsi = 0
            ServerOutBuf.lines[self.server_id].append(self.line_buffer)

            self.new_line_handler(self.line_buffer)
            self.line_buffer = ""
            # Limit list length to self.max_lines:
            if len(ServerOutBuf.lines[self.server_id]) > self.max_lines:
                ServerOutBuf.lines[self.server_id].pop(0)

    def check(self):
        while True:
            if self.proc.poll() is None:
                char = self.proc.stdout.read(1).decode("utf-8", "ignore")
                # TODO: we may want to benchmark reading in blocks and userspace
                # processing it later, reads are kind of expensive as a syscall
                self.process_byte(char)
            else:
                flush = self.proc.stdout.read().decode("utf-8", "ignore")
                for char in flush:
                    self.process_byte(char)
                break

    def new_line_handler(self, new_line):
        new_line = re.sub("(\033\\[(0;)?[0-9]*[A-z]?(;[0-9])?m?)", " ", new_line)
        new_line = re.sub("[A-z]{2}\b\b", "", new_line)
        highlighted = self.helper.log_colors(html.escape(new_line))

        logger.debug("Broadcasting new virtual terminal line")

        # TODO: Do not send data to clients who do not have permission to view
        # this server's console
        self.helper.websocket_helper.broadcast_page_params(
            "/panel/server_detail",
            {"id": self.server_id},
            "vterm_new_line",
            {"line": highlighted + "<br />"},
        )


# **********************************************************************************
#                               Minecraft Server Class
# **********************************************************************************
class Server:
    def __init__(self, helper, management_helper, stats):
        self.helper = helper
        self.management_helper = management_helper
        # holders for our process
        self.process = None
        self.line = False
        self.start_time = None
        self.server_command = None
        self.server_path = None
        self.server_thread = None
        self.settings = None
        self.updating = False
        self.server_id = None
        self.jar_update_url = None
        self.name = None
        self.is_crashed = False
        self.restart_count = 0
        self.stats = stats
        tz = get_localzone()
        self.server_scheduler = BackgroundScheduler(timezone=str(tz))
        self.server_scheduler.start()
        self.backup_thread = threading.Thread(
            target=self.a_backup_server, daemon=True, name=f"backup_{self.name}"
        )
        self.is_backingup = False
        # Reset crash and update at initialization
        helper_servers.server_crash_reset(self.server_id)
        helper_servers.set_update(self.server_id, False)

    # **********************************************************************************
    #                               Minecraft Server Management
    # **********************************************************************************
    def reload_server_settings(self):
        server_data = helper_servers.get_server_data_by_id(self.server_id)
        self.settings = server_data

    def do_server_setup(self, server_data_obj):
        serverId = server_data_obj["server_id"]
        serverName = server_data_obj["server_name"]
        autoStart = server_data_obj["auto_start"]

        logger.info(
            f"Creating Server object: {serverId} | "
            f"Server Name: {serverName} | "
            f"Auto Start: {autoStart}"
        )
        self.server_id = serverId
        self.name = serverName
        self.settings = server_data_obj

        self.record_server_stats()

        # build our server run command

        if server_data_obj["auto_start"]:
            delay = int(self.settings["auto_start_delay"])

            logger.info(f"Scheduling server {self.name} to start in {delay} seconds")
            Console.info(f"Scheduling server {self.name} to start in {delay} seconds")

            self.server_scheduler.add_job(
                self.run_scheduled_server,
                "interval",
                seconds=delay,
                id=str(self.server_id),
            )

    def run_scheduled_server(self):
        Console.info(f"Starting server ID: {self.server_id} - {self.name}")
        logger.info(f"Starting server ID: {self.server_id} - {self.name}")
        # Sets waiting start to false since we're attempting to start the server.
        helper_servers.set_waiting_start(self.server_id, False)
        self.run_threaded_server(None)

        # remove the scheduled job since it's ran
        return self.server_scheduler.remove_job(str(self.server_id))

    def run_threaded_server(self, user_id):
        # start the server
        self.server_thread = threading.Thread(
            target=self.start_server,
            daemon=True,
            args=(user_id,),
            name=f"{self.server_id}_server_thread",
        )
        self.server_thread.start()

        # Register an shedule for polling server stats when running
        logger.info(f"Polling server statistics {self.name} every {5} seconds")
        Console.info(f"Polling server statistics {self.name} every {5} seconds")
        try:
            self.server_scheduler.add_job(
                self.realtime_stats,
                "interval",
                seconds=5,
                id="stats_" + str(self.server_id),
            )
        except:
            self.server_scheduler.remove_job("stats_" + str(self.server_id))
            self.server_scheduler.add_job(
                self.realtime_stats,
                "interval",
                seconds=5,
                id="stats_" + str(self.server_id),
            )

    def setup_server_run_command(self):
        # configure the server
        server_exec_path = Helpers.get_os_understandable_path(
            self.settings["executable"]
        )
        self.server_command = Helpers.cmdparse(self.settings["execution_command"])
        self.server_path = Helpers.get_os_understandable_path(self.settings["path"])

        # let's do some quick checking to make sure things actually exists
        full_path = os.path.join(self.server_path, server_exec_path)
        if not Helpers.check_file_exists(full_path):
            logger.critical(
                f"Server executable path: {full_path} does not seem to exist"
            )
            Console.critical(
                f"Server executable path: {full_path} does not seem to exist"
            )

        if not Helpers.check_path_exists(self.server_path):
            logger.critical(f"Server path: {self.server_path} does not seem to exits")
            Console.critical(f"Server path: {self.server_path} does not seem to exits")

        if not Helpers.check_writeable(self.server_path):
            logger.critical(f"Unable to write/access {self.server_path}")
            Console.critical(f"Unable to write/access {self.server_path}")

    def start_server(self, user_id):
        if not user_id:
            user_lang = self.helper.get_setting("language")
        else:
            user_lang = helper_users.get_user_lang_by_id(user_id)

        if helper_servers.get_download_status(self.server_id):
            if user_id:
                self.helper.websocket_helper.broadcast_user(
                    user_id,
                    "send_start_error",
                    {
                        "error": self.helper.translation.translate(
                            "error", "not-downloaded", user_lang
                        )
                    },
                )
            return False

        logger.info(
            f"Start command detected. Reloading settings from DB for server {self.name}"
        )
        self.setup_server_run_command()
        # fail safe in case we try to start something already running
        if self.check_running():
            logger.error("Server is already running - Cancelling Startup")
            Console.error("Server is already running - Cancelling Startup")
            return False
        if self.check_update():
            logger.error("Server is updating. Terminating startup.")
            return False

        logger.info(f"Launching Server {self.name} with command {self.server_command}")
        Console.info(f"Launching Server {self.name} with command {self.server_command}")

        # Checks for eula. Creates one if none detected.
        # If EULA is detected and not set to true we offer to set it true.
        if Helpers.check_file_exists(os.path.join(self.settings["path"], "eula.txt")):
            f = open(
                os.path.join(self.settings["path"], "eula.txt"), "r", encoding="utf-8"
            )
            line = f.readline().lower()
            if line == "eula=true":
                e_flag = True

            elif line == "eula = true":
                e_flag = True

            elif line == "eula= true":
                e_flag = True

            elif line == "eula =true":
                e_flag = True

            else:
                e_flag = False
        else:
            e_flag = False

        if not e_flag:
            if user_id:
                self.helper.websocket_helper.broadcast_user(
                    user_id, "send_eula_bootbox", {"id": self.server_id}
                )
            else:
                logger.error(
                    "Autostart failed due to EULA being false. "
                    "Agree not sent due to auto start."
                )
                return False
            return False
        f.close()
        if Helpers.is_os_windows():
            logger.info("Windows Detected")
        else:
            logger.info("Unix Detected")

        logger.info(
            f"Starting server in {self.server_path} with command: {self.server_command}"
        )

        # checks to make sure file is openable (downloaded) and exists.
        try:
            f = open(
                os.path.join(
                    self.server_path,
                    helper_servers.get_server_data_by_id(self.server_id)["executable"],
                ),
                "r",
                encoding="utf-8",
            )
            f.close()

        except:
            if user_id:
                self.helper.websocket_helper.broadcast_user(
                    user_id,
                    "send_start_error",
                    {
                        "error": self.helper.translation.translate(
                            "error", "not-downloaded", user_lang
                        )
                    },
                )
            return

        if (
            not Helpers.is_os_windows()
            and helper_servers.get_server_type_by_id(self.server_id)
            == "minecraft-bedrock"
        ):
            logger.info(
                f"Bedrock and Unix detected for server {self.name}. "
                f"Switching to appropriate execution string"
            )
            my_env = os.environ
            my_env["LD_LIBRARY_PATH"] = self.server_path
            try:
                self.process = subprocess.Popen(
                    self.server_command,
                    cwd=self.server_path,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=my_env,
                )
            except Exception as ex:
                logger.error(
                    f"Server {self.name} failed to start with error code: {ex}"
                )
                if user_id:
                    self.helper.websocket_helper.broadcast_user(
                        user_id,
                        "send_start_error",
                        {
                            "error": self.helper.translation.translate(
                                "error", "start-error", user_lang
                            ).format(self.name, ex)
                        },
                    )
                return False

        else:
            try:
                self.process = subprocess.Popen(
                    self.server_command,
                    cwd=self.server_path,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
            except Exception as ex:
                # Checks for java on initial fail
                if os.system("java -version") == 32512:
                    if user_id:
                        self.helper.websocket_helper.broadcast_user(
                            user_id,
                            "send_start_error",
                            {
                                "error": self.helper.translation.translate(
                                    "error", "noJava", user_lang
                                ).format(self.name)
                            },
                        )
                    return False
                else:
                    logger.error(
                        f"Server {self.name} failed to start with error code: {ex}"
                    )
                if user_id:
                    self.helper.websocket_helper.broadcast_user(
                        user_id,
                        "send_start_error",
                        {
                            "error": self.helper.translation.translate(
                                "error", "start-error", user_lang
                            ).format(self.name, ex)
                        },
                    )
                return False

        out_buf = ServerOutBuf(self.helper, self.process, self.server_id)

        logger.debug(f"Starting virtual terminal listener for server {self.name}")
        threading.Thread(
            target=out_buf.check, daemon=True, name=f"{self.server_id}_virtual_terminal"
        ).start()

        self.is_crashed = False
        helper_servers.server_crash_reset(self.server_id)

        self.start_time = str(datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

        if self.process.poll() is None:
            logger.info(f"Server {self.name} running with PID {self.process.pid}")
            Console.info(f"Server {self.name} running with PID {self.process.pid}")
            self.is_crashed = False
            helper_servers.server_crash_reset(self.server_id)
            self.record_server_stats()
            check_internet_thread = threading.Thread(
                target=self.check_internet_thread,
                daemon=True,
                args=(
                    user_id,
                    user_lang,
                ),
                name=f"{self.name}_Internet",
            )
            check_internet_thread.start()
            # Checks if this is the servers first run.
            if helper_servers.get_first_run(self.server_id):
                helper_servers.set_first_run(self.server_id)
                loc_server_port = helper_servers.get_server_stats_by_id(self.server_id)[
                    "server_port"
                ]
                # Sends port reminder message.
                self.helper.websocket_helper.broadcast_user(
                    user_id,
                    "send_start_error",
                    {
                        "error": self.helper.translation.translate(
                            "error", "portReminder", user_lang
                        ).format(self.name, loc_server_port)
                    },
                )
                server_users = Permissions_Servers.get_server_user_list(self.server_id)
                for user in server_users:
                    if user != user_id:
                        self.helper.websocket_helper.broadcast_user(
                            user, "send_start_reload", {}
                        )
            else:
                server_users = Permissions_Servers.get_server_user_list(self.server_id)
                for user in server_users:
                    self.helper.websocket_helper.broadcast_user(
                        user, "send_start_reload", {}
                    )
        else:
            logger.warning(
                f"Server PID {self.process.pid} died right after starting "
                f"- is this a server config issue?"
            )
            Console.critical(
                f"Server PID {self.process.pid} died right after starting "
                f"- is this a server config issue?"
            )

        if self.settings["crash_detection"]:
            logger.info(
                f"Server {self.name} has crash detection enabled "
                f"- starting watcher task"
            )
            Console.info(
                f"Server {self.name} has crash detection enabled "
                f"- starting watcher task"
            )

            self.server_scheduler.add_job(
                self.detect_crash, "interval", seconds=30, id=f"c_{self.server_id}"
            )

    def check_internet_thread(self, user_id, user_lang):
        if user_id:
            if not Helpers.check_internet():
                self.helper.websocket_helper.broadcast_user(
                    user_id,
                    "send_start_error",
                    {
                        "error": self.helper.translation.translate(
                            "error", "internet", user_lang
                        )
                    },
                )

    def stop_crash_detection(self):
        # This is only used if the crash detection settings change
        # while the server is running.
        if self.check_running():
            logger.info(f"Detected crash detection shut off for server {self.name}")
            try:
                self.server_scheduler.remove_job("c_" + str(self.server_id))
            except:
                logger.error(
                    f"Removing crash watcher for server {self.name} failed. "
                    f"Assuming it was never started."
                )

    def start_crash_detection(self):
        # This is only used if the crash detection settings change
        # while the server is running.
        if self.check_running():
            logger.info(
                f"Server {self.name} has crash detection enabled "
                f"- starting watcher task"
            )
            Console.info(
                f"Server {self.name} has crash detection enabled "
                "- starting watcher task"
            )
            self.server_scheduler.add_job(
                self.detect_crash, "interval", seconds=30, id=f"c_{self.server_id}"
            )

    def stop_threaded_server(self):
        self.stop_server()

        if self.server_thread:
            self.server_thread.join()

    def stop_server(self):
        if self.settings["stop_command"]:
            self.send_command(self.settings["stop_command"])
            if self.settings["crash_detection"]:
                # remove crash detection watcher
                logger.info(f"Removing crash watcher for server {self.name}")
                try:
                    self.server_scheduler.remove_job("c_" + str(self.server_id))
                except:
                    logger.error(
                        f"Removing crash watcher for server {self.name} failed. "
                        f"Assuming it was never started."
                    )
        else:
            # windows will need to be handled separately for Ctrl+C
            self.process.terminate()
        running = self.check_running()
        if not running:
            logger.info(f"Can't stop server {self.name} if it's not running")
            Console.info(f"Can't stop server {self.name} if it's not running")
            return
        x = 0

        # caching the name and pid number
        server_name = self.name
        server_pid = self.process.pid

        while running:
            x = x + 1
            logstr = (
                f"Server {server_name} is still running "
                f"- waiting 2s to see if it stops ({int(60-(x*2))} "
                f"seconds until force close)"
            )
            logger.info(logstr)
            Console.info(logstr)
            running = self.check_running()
            time.sleep(2)

            # if we haven't closed in 60 seconds, let's just slam down on the PID
            if x >= 30:
                logger.info(
                    f"Server {server_name} is still running - Forcing the process down"
                )
                Console.info(
                    f"Server {server_name} is still running - Forcing the process down"
                )
                self.kill()

        logger.info(f"Stopped Server {server_name} with PID {server_pid}")
        Console.info(f"Stopped Server {server_name} with PID {server_pid}")

        # massive resetting of variables
        self.cleanup_server_object()
        server_users = Permissions_Servers.get_server_user_list(self.server_id)

        # remove the stats polling job since server is stopped
        self.server_scheduler.remove_job("stats_" + str(self.server_id))

        self.record_server_stats()

        for user in server_users:
            self.helper.websocket_helper.broadcast_user(user, "send_start_reload", {})

    def restart_threaded_server(self, user_id):
        # if not already running, let's just start
        if not self.check_running():
            self.run_threaded_server(user_id)
        else:
            self.stop_threaded_server()
            time.sleep(2)
            self.run_threaded_server(user_id)

    def cleanup_server_object(self):
        self.start_time = None
        self.restart_count = 0
        self.is_crashed = False
        self.updating = False
        self.process = None

    def check_running(self):
        # if process is None, we never tried to start
        if self.process is None:
            return False
        poll = self.process.poll()
        if poll is None:
            return True
        else:
            self.last_rc = poll
            return False

    def send_command(self, command):
        if not self.check_running() and command.lower() != "start":
            logger.warning(f'Server not running, unable to send command "{command}"')
            return False
        Console.info(f"COMMAND TIME: {command}")
        logger.debug(f"Sending command {command} to server")

        # send it
        self.process.stdin.write(f"{command}\n".encode("utf-8"))
        self.process.stdin.flush()

    def crash_detected(self, name):

        # clear the old scheduled watcher task
        self.server_scheduler.remove_job(f"c_{self.server_id}")
        # remove the stats polling job since server is stopped
        self.server_scheduler.remove_job("stats_" + str(self.server_id))

        # the server crashed, or isn't found - so let's reset things.
        logger.warning(
            f"The server {name} seems to have vanished unexpectedly, did it crash?"
        )

        if self.settings["crash_detection"]:
            logger.warning(
                f"The server {name} has crashed and will be restarted. "
                f"Restarting server"
            )
            Console.critical(
                f"The server {name} has crashed and will be restarted. "
                f"Restarting server"
            )
            self.run_threaded_server(None)
            return True
        else:
            logger.critical(
                f"The server {name} has crashed, "
                f"crash detection is disabled and it will not be restarted"
            )
            Console.critical(
                f"The server {name} has crashed, "
                f"crash detection is disabled and it will not be restarted"
            )
            return False

    def kill(self):
        logger.info(f"Terminating server {self.server_id} and all child processes")
        process = psutil.Process(self.process.pid)

        # for every sub process...
        for proc in process.children(recursive=True):
            # kill all the child processes
            logger.info(f"Sending SIGKILL to server {proc.name}")
            proc.kill()
        # kill the main process we are after
        logger.info("Sending SIGKILL to parent")
        self.server_scheduler.remove_job("stats_" + str(self.server_id))
        self.process.kill()

    def get_start_time(self):
        if self.check_running():
            return self.start_time
        else:
            return False

    def get_pid(self):
        if self.process is not None:
            return self.process.pid
        else:
            return None

    def detect_crash(self):

        logger.info(f"Detecting possible crash for server: {self.name} ")

        running = self.check_running()

        # if all is okay, we just exit out
        if running:
            return
        # check the exit code -- This could be a fix for /stop
        if self.process.returncode == 0:
            logger.warning(
                f"Process {self.process.pid} exited with code "
                f"{self.process.returncode}. This is considered a clean exit"
                f" supressing crash handling."
            )
            # cancel the watcher task
            self.server_scheduler.remove_job("c_" + str(self.server_id))
            return

        helper_servers.sever_crashed(self.server_id)
        # if we haven't tried to restart more 3 or more times
        if self.restart_count <= 3:

            # start the server if needed
            server_restarted = self.crash_detected(self.name)

            if server_restarted:
                # add to the restart count
                self.restart_count = self.restart_count + 1

        # we have tried to restart 4 times...
        elif self.restart_count == 4:
            logger.critical(
                f"Server {self.name} has been restarted {self.restart_count}"
                f" times. It has crashed, not restarting."
            )
            Console.critical(
                f"Server {self.name} has been restarted {self.restart_count}"
                f" times. It has crashed, not restarting."
            )

            self.restart_count = 0
            self.is_crashed = True
            helper_servers.sever_crashed(self.server_id)

            # cancel the watcher task
            self.server_scheduler.remove_job("c_" + str(self.server_id))

    def remove_watcher_thread(self):
        logger.info("Removing old crash detection watcher thread")
        Console.info("Removing old crash detection watcher thread")
        self.server_scheduler.remove_job("c_" + str(self.server_id))

    def agree_eula(self, user_id):
        file = os.path.join(self.server_path, "eula.txt")
        f = open(file, "w", encoding="utf-8")
        f.write("eula=true")
        f.close()
        self.run_threaded_server(user_id)

    def is_backup_running(self):
        if self.is_backingup:
            return True
        else:
            return False

    def backup_server(self):
        if self.settings["backup_path"] == "":
            return
        backup_thread = threading.Thread(
            target=self.a_backup_server, daemon=True, name=f"backup_{self.name}"
        )
        logger.info(
            f"Starting Backup Thread for server {self.settings['server_name']}."
        )
        if self.server_path is None:
            self.server_path = Helpers.get_os_understandable_path(self.settings["path"])
            logger.info(
                "Backup Thread - Local server path not defined. "
                "Setting local server path variable."
            )
        # checks if the backup thread is currently alive for this server
        if not self.is_backingup:
            try:
                backup_thread.start()
                self.is_backingup = True
            except Exception as ex:
                logger.error(f"Failed to start backup: {ex}")
                return False
        else:
            logger.error(
                f"Backup is already being processed for server "
                f"{self.settings['server_name']}. Canceling backup request"
            )
            return False
        logger.info(f"Backup Thread started for server {self.settings['server_name']}.")

    def a_backup_server(self):
        if len(self.helper.websocket_helper.clients) > 0:
            self.helper.websocket_helper.broadcast_page_params(
                "/panel/server_detail",
                {"id": str(self.server_id)},
                "backup_reload",
                {"percent": 0, "total_files": 0},
            )
        logger.info(f"Starting server {self.name} (ID {self.server_id}) backup")
        server_users = Permissions_Servers.get_server_user_list(self.server_id)
        for user in server_users:
            self.helper.websocket_helper.broadcast_user(
                user,
                "notification",
                self.helper.translation.translate(
                    "notify", "backupStarted", helper_users.get_user_lang_by_id(user)
                ).format(self.name),
            )
        time.sleep(3)
        conf = helpers_management.get_backup_config(self.server_id)
        self.helper.ensure_dir_exists(self.settings["backup_path"])
        try:
            backup_filename = (
                f"{self.settings['backup_path']}/"
                f"{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
            )
            logger.info(
                f"Creating backup of server '{self.settings['server_name']}'"
                f" (ID#{self.server_id}, path={self.server_path}) "
                f"at '{backup_filename}'"
            )

            tempDir = tempfile.mkdtemp()
            self.server_scheduler.add_job(
                self.backup_status,
                "interval",
                seconds=1,
                id="backup_" + str(self.server_id),
                args=[tempDir + "/", backup_filename + ".zip"],
            )
            # pylint: disable=unexpected-keyword-arg
            FileHelpers.copy_dir(self.server_path, tempDir, dirs_exist_ok=True)
            excluded_dirs = helpers_management.get_excluded_backup_dirs(self.server_id)
            server_dir = Helpers.get_os_understandable_path(self.settings["path"])

            for my_dir in excluded_dirs:
                # Take the full path of the excluded dir and replace the
                # server path with the temp path, this is so that we're
                # only deleting excluded dirs from the temp path
                # and not the server path
                excluded_dir = Helpers.get_os_understandable_path(my_dir).replace(
                    server_dir, Helpers.get_os_understandable_path(tempDir)
                )
                # Next, check to see if it is a directory
                if os.path.isdir(excluded_dir):
                    # If it is a directory,
                    # recursively delete the entire directory from the backup
                    FileHelpers.del_dirs(excluded_dir)
                else:
                    # If not, just remove the file
                    os.remove(excluded_dir)
            if conf["compress"]:
                logger.debug(
                    "Found compress backup to be true. Calling compressed archive"
                )
                FileHelpers.make_compressed_archive(
                    Helpers.get_os_understandable_path(backup_filename), tempDir
                )
            else:
                logger.debug(
                    "Found compress backup to be false. Calling NON-compressed archive"
                )
                FileHelpers.make_archive(
                    Helpers.get_os_understandable_path(backup_filename), tempDir
                )

            while (
                len(self.list_backups()) > conf["max_backups"]
                and conf["max_backups"] > 0
            ):
                backup_list = self.list_backups()
                oldfile = backup_list[0]
                oldfile_path = f"{conf['backup_path']}/{oldfile['path']}"
                logger.info(f"Removing old backup '{oldfile['path']}'")
                os.remove(Helpers.get_os_understandable_path(oldfile_path))

            self.is_backingup = False
            FileHelpers.del_dirs(tempDir)
            logger.info(f"Backup of server: {self.name} completed")
            self.server_scheduler.remove_job("backup_" + str(self.server_id))
            results = {"percent": 100, "total_files": 0, "current_file": 0}
            if len(self.helper.websocket_helper.clients) > 0:
                self.helper.websocket_helper.broadcast_page_params(
                    "/panel/server_detail",
                    {"id": str(self.server_id)},
                    "backup_status",
                    results,
                )
            server_users = Permissions_Servers.get_server_user_list(self.server_id)
            for user in server_users:
                self.helper.websocket_helper.broadcast_user(
                    user,
                    "notification",
                    self.helper.translation.translate(
                        "notify",
                        "backupComplete",
                        helper_users.get_user_lang_by_id(user),
                    ).format(self.name),
                )
            time.sleep(3)
            return
        except:
            logger.exception(
                f"Failed to create backup of server {self.name} (ID {self.server_id})"
            )
            self.server_scheduler.remove_job("backup_" + str(self.server_id))
            results = {"percent": 100, "total_files": 0, "current_file": 0}
            if len(self.helper.websocket_helper.clients) > 0:
                self.helper.websocket_helper.broadcast_page_params(
                    "/panel/server_detail",
                    {"id": str(self.server_id)},
                    "backup_status",
                    results,
                )
            self.is_backingup = False
            return

    def backup_status(self, source_path, dest_path):
        results = Helpers.calc_percent(source_path, dest_path)
        self.backup_stats = results
        if len(self.helper.websocket_helper.clients) > 0:
            self.helper.websocket_helper.broadcast_page_params(
                "/panel/server_detail",
                {"id": str(self.server_id)},
                "backup_status",
                results,
            )

    def send_backup_status(self):
        try:
            return self.backup_stats
        except:
            return {"percent": 0, "total_files": 0}

    def list_backups(self):
        if self.settings["backup_path"]:
            if Helpers.check_path_exists(
                Helpers.get_os_understandable_path(self.settings["backup_path"])
            ):
                files = Helpers.get_human_readable_files_sizes(
                    Helpers.list_dir_by_date(
                        Helpers.get_os_understandable_path(self.settings["backup_path"])
                    )
                )
                return [
                    {
                        "path": os.path.relpath(
                            f["path"],
                            start=Helpers.get_os_understandable_path(
                                self.settings["backup_path"]
                            ),
                        ),
                        "size": f["size"],
                    }
                    for f in files
                ]
            else:
                return []
        else:
            logger.info(
                f"Error putting backup file list for server with ID: {self.server_id}"
            )
            return []

    def jar_update(self):
        helper_servers.set_update(self.server_id, True)
        update_thread = threading.Thread(
            target=self.a_jar_update, daemon=True, name=f"exe_update_{self.name}"
        )
        update_thread.start()

    def check_update(self):

        if helper_servers.get_server_stats_by_id(self.server_id)["updating"]:
            return True
        else:
            return False

    def a_jar_update(self):
        wasStarted = "-1"
        self.backup_server()
        # checks if server is running. Calls shutdown if it is running.
        if self.check_running():
            wasStarted = True
            logger.info(
                f"Server with PID {self.process.pid} is running. "
                f"Sending shutdown command"
            )
            self.stop_threaded_server()
        else:
            wasStarted = False
        if len(self.helper.websocket_helper.clients) > 0:
            # There are clients
            self.check_update()
            message = (
                '<a data-id="' + str(self.server_id) + '" class=""> UPDATING...</i></a>'
            )
            self.helper.websocket_helper.broadcast_page(
                "/panel/server_detail",
                "update_button_status",
                {
                    "isUpdating": self.check_update(),
                    "server_id": self.server_id,
                    "wasRunning": wasStarted,
                    "string": message,
                },
            )
            self.helper.websocket_helper.broadcast_page(
                "/panel/dashboard", "send_start_reload", {}
            )
        backup_dir = os.path.join(
            Helpers.get_os_understandable_path(self.settings["path"]),
            "crafty_executable_backups",
        )
        # checks if backup directory already exists
        if os.path.isdir(backup_dir):
            backup_executable = os.path.join(backup_dir, "old_server.jar")
        else:
            logger.info(
                f"Executable backup directory not found for Server: {self.name}."
                f" Creating one."
            )
            os.mkdir(backup_dir)
            backup_executable = os.path.join(backup_dir, "old_server.jar")

            if os.path.isfile(backup_executable):
                # removes old backup
                logger.info(f"Old backup found for server: {self.name}. Removing...")
                os.remove(backup_executable)
                logger.info(f"Old backup removed for server: {self.name}.")
            else:
                logger.info(f"No old backups found for server: {self.name}")

        current_executable = os.path.join(
            Helpers.get_os_understandable_path(self.settings["path"]),
            self.settings["executable"],
        )

        # copies to backup dir
        Helpers.copy_files(current_executable, backup_executable)

        # boolean returns true for false for success
        downloaded = Helpers.download_file(
            self.settings["executable_update_url"], current_executable
        )

        while helper_servers.get_server_stats_by_id(self.server_id)["updating"]:
            if downloaded and not self.is_backingup:
                logger.info("Executable updated successfully. Starting Server")

                helper_servers.set_update(self.server_id, False)
                if len(self.helper.websocket_helper.clients) > 0:
                    # There are clients
                    self.check_update()
                    server_users = Permissions_Servers.get_server_user_list(
                        self.server_id
                    )
                    for user in server_users:
                        self.helper.websocket_helper.broadcast_user(
                            user,
                            "notification",
                            "Executable update finished for " + self.name,
                        )
                    time.sleep(3)
                    self.helper.websocket_helper.broadcast_page(
                        "/panel/server_detail",
                        "update_button_status",
                        {
                            "isUpdating": self.check_update(),
                            "server_id": self.server_id,
                            "wasRunning": wasStarted,
                        },
                    )
                    self.helper.websocket_helper.broadcast_page(
                        "/panel/dashboard", "send_start_reload", {}
                    )
                server_users = Permissions_Servers.get_server_user_list(self.server_id)
                for user in server_users:
                    self.helper.websocket_helper.broadcast_user(
                        user,
                        "notification",
                        "Executable update finished for " + self.name,
                    )

                self.management_helper.add_to_audit_log_raw(
                    "Alert",
                    "-1",
                    self.server_id,
                    "Executable update finished for " + self.name,
                    self.settings["server_ip"],
                )
                if wasStarted:
                    self.start_server()
            elif not downloaded and not self.is_backingup:
                time.sleep(5)
                server_users = Permissions_Servers.get_server_user_list(self.server_id)
                for user in server_users:
                    self.helper.websocket_helper.broadcast_user(
                        user,
                        "notification",
                        "Executable update failed for "
                        + self.name
                        + ". Check log file for details.",
                    )
                logger.error("Executable download failed.")

    # **********************************************************************************
    #                               Minecraft Servers Statistics
    # **********************************************************************************

    def realtime_stats(self):
        total_players = 0
        max_players = 0
        servers_ping = []
        raw_ping_result = []
        raw_ping_result = self.get_raw_server_stats(self.server_id)

        if f"{raw_ping_result.get('icon')}" == "b''":
            raw_ping_result["icon"] = False

        servers_ping.append(
            {
                "id": raw_ping_result.get("id"),
                "started": raw_ping_result.get("started"),
                "running": raw_ping_result.get("running"),
                "cpu": raw_ping_result.get("cpu"),
                "mem": raw_ping_result.get("mem"),
                "mem_percent": raw_ping_result.get("mem_percent"),
                "world_name": raw_ping_result.get("world_name"),
                "world_size": raw_ping_result.get("world_size"),
                "server_port": raw_ping_result.get("server_port"),
                "int_ping_results": raw_ping_result.get("int_ping_results"),
                "online": raw_ping_result.get("online"),
                "max": raw_ping_result.get("max"),
                "players": raw_ping_result.get("players"),
                "desc": raw_ping_result.get("desc"),
                "version": raw_ping_result.get("version"),
                "icon": raw_ping_result.get("icon"),
            }
        )
        if len(self.helper.websocket_helper.clients) > 0:
            self.helper.websocket_helper.broadcast_page_params(
                "/panel/server_detail",
                {"id": str(self.server_id)},
                "update_server_details",
                {
                    "id": raw_ping_result.get("id"),
                    "started": raw_ping_result.get("started"),
                    "running": raw_ping_result.get("running"),
                    "cpu": raw_ping_result.get("cpu"),
                    "mem": raw_ping_result.get("mem"),
                    "mem_percent": raw_ping_result.get("mem_percent"),
                    "world_name": raw_ping_result.get("world_name"),
                    "world_size": raw_ping_result.get("world_size"),
                    "server_port": raw_ping_result.get("server_port"),
                    "int_ping_results": raw_ping_result.get("int_ping_results"),
                    "online": raw_ping_result.get("online"),
                    "max": raw_ping_result.get("max"),
                    "players": raw_ping_result.get("players"),
                    "desc": raw_ping_result.get("desc"),
                    "version": raw_ping_result.get("version"),
                    "icon": raw_ping_result.get("icon"),
                },
            )
        total_players += int(raw_ping_result.get("online"))
        max_players += int(raw_ping_result.get("max"))

        self.record_server_stats()

        if (len(servers_ping) > 0) & (len(self.helper.websocket_helper.clients) > 0):
            try:
                self.helper.websocket_helper.broadcast_page(
                    "/panel/dashboard", "update_server_status", servers_ping
                )
                self.helper.websocket_helper.broadcast_page(
                    "/status", "update_server_status", servers_ping
                )
            except:
                Console.critical("Can't broadcast server status to websocket")

    def get_servers_stats(self):

        server_stats = {}

        logger.info("Getting Stats for Server " + self.name + " ...")

        server_id = self.server_id
        server = helper_servers.get_server_data_by_id(server_id)

        logger.debug(f"Getting stats for server: {server_id}")

        # get our server object, settings and data dictionaries
        self.reload_server_settings()

        # world data
        server_path = server["path"]

        # process stats
        p_stats = Stats._get_process_stats(self.process)

        # TODO: search server properties file for possible override of 127.0.0.1
        internal_ip = server["server_ip"]
        server_port = server["server_port"]
        server_name = server.get("server_name", f"ID#{server_id}")

        logger.debug(f"Pinging server '{server}' on {internal_ip}:{server_port}")
        if helper_servers.get_server_type_by_id(server_id) == "minecraft-bedrock":
            int_mc_ping = ping_bedrock(internal_ip, int(server_port))
        else:
            int_mc_ping = ping(internal_ip, int(server_port))

        int_data = False
        ping_data = {}

        # if we got a good ping return, let's parse it
        if int_mc_ping:
            int_data = True
            if (
                helper_servers.get_server_type_by_id(server["server_id"])
                == "minecraft-bedrock"
            ):
                ping_data = Stats.parse_server_RakNet_ping(int_mc_ping)
            else:
                ping_data = Stats.parse_server_ping(int_mc_ping)
        # Makes sure we only show stats when a server is online
        # otherwise people have gotten confused.
        if self.check_running():
            server_stats = {
                "id": server_id,
                "started": self.get_start_time(),
                "running": self.check_running(),
                "cpu": p_stats.get("cpu_usage", 0),
                "mem": p_stats.get("memory_usage", 0),
                "mem_percent": p_stats.get("mem_percentage", 0),
                "world_name": server_name,
                "world_size": Stats.get_world_size(server_path),
                "server_port": server_port,
                "int_ping_results": int_data,
                "online": ping_data.get("online", False),
                "max": ping_data.get("max", False),
                "players": ping_data.get("players", False),
                "desc": ping_data.get("server_description", False),
                "version": ping_data.get("server_version", False),
            }
        else:
            server_stats = {
                "id": server_id,
                "started": self.get_start_time(),
                "running": self.check_running(),
                "cpu": p_stats.get("cpu_usage", 0),
                "mem": p_stats.get("memory_usage", 0),
                "mem_percent": p_stats.get("mem_percentage", 0),
                "world_name": server_name,
                "world_size": Stats.get_world_size(server_path),
                "server_port": server_port,
                "int_ping_results": int_data,
                "online": False,
                "max": False,
                "players": False,
                "desc": False,
                "version": False,
            }

        return server_stats

    def get_server_players(self):

        server = helper_servers.get_server_data_by_id(self.server_id)

        logger.info(f"Getting players for server {server}")

        # get our settings and data dictionaries
        # server_settings = server.get('server_settings', {})
        # server_data = server.get('server_data_obj', {})

        # TODO: search server properties file for possible override of 127.0.0.1
        internal_ip = server["server_ip"]
        server_port = server["server_port"]

        logger.debug(f"Pinging {internal_ip} on port {server_port}")
        if helper_servers.get_server_type_by_id(self.server_id) != "minecraft-bedrock":
            int_mc_ping = ping(internal_ip, int(server_port))

            ping_data = {}

            # if we got a good ping return, let's parse it
            if int_mc_ping:
                ping_data = Stats.parse_server_ping(int_mc_ping)
                return ping_data["players"]
        return []

    def get_raw_server_stats(self, server_id):

        try:
            server = helper_servers.get_server_obj(server_id)
        except:
            return {
                "id": server_id,
                "started": False,
                "running": False,
                "cpu": 0,
                "mem": 0,
                "mem_percent": 0,
                "world_name": None,
                "world_size": None,
                "server_port": None,
                "int_ping_results": False,
                "online": False,
                "max": False,
                "players": False,
                "desc": False,
                "version": False,
                "icon": False,
            }

        server_stats = {}
        server = helper_servers.get_server_obj(server_id)
        if not server:
            return {}
        server_dt = helper_servers.get_server_data_by_id(server_id)

        logger.debug(f"Getting stats for server: {server_id}")

        # get our server object, settings and data dictionaries
        self.reload_server_settings()

        # world data
        server_name = server_dt["server_name"]
        server_path = server_dt["path"]

        # process stats
        p_stats = Stats._get_process_stats(self.process)

        # TODO: search server properties file for possible override of 127.0.0.1
        # internal_ip =   server['server_ip']
        # server_port = server['server_port']
        internal_ip = server_dt["server_ip"]
        server_port = server_dt["server_port"]

        logger.debug(f"Pinging server '{self.name}' on {internal_ip}:{server_port}")
        if helper_servers.get_server_type_by_id(server_id) == "minecraft-bedrock":
            int_mc_ping = ping_bedrock(internal_ip, int(server_port))
        else:
            int_mc_ping = ping(internal_ip, int(server_port))

        int_data = False
        ping_data = {}
        # Makes sure we only show stats when a server is online
        # otherwise people have gotten confused.
        if self.check_running():
            # if we got a good ping return, let's parse it
            if helper_servers.get_server_type_by_id(server_id) != "minecraft-bedrock":
                if int_mc_ping:
                    int_data = True
                    ping_data = Stats.parse_server_ping(int_mc_ping)

                server_stats = {
                    "id": server_id,
                    "started": self.get_start_time(),
                    "running": self.check_running(),
                    "cpu": p_stats.get("cpu_usage", 0),
                    "mem": p_stats.get("memory_usage", 0),
                    "mem_percent": p_stats.get("mem_percentage", 0),
                    "world_name": server_name,
                    "world_size": Stats.get_world_size(server_path),
                    "server_port": server_port,
                    "int_ping_results": int_data,
                    "online": ping_data.get("online", False),
                    "max": ping_data.get("max", False),
                    "players": ping_data.get("players", False),
                    "desc": ping_data.get("server_description", False),
                    "version": ping_data.get("server_version", False),
                    "icon": ping_data.get("server_icon", False),
                }

            else:
                if int_mc_ping:
                    int_data = True
                    ping_data = Stats.parse_server_RakNet_ping(int_mc_ping)
                    try:
                        server_icon = base64.encodebytes(ping_data["icon"])
                    except Exception as ex:
                        server_icon = False
                        logger.info(f"Unable to read the server icon : {ex}")

                    server_stats = {
                        "id": server_id,
                        "started": self.get_start_time(),
                        "running": self.check_running(),
                        "cpu": p_stats.get("cpu_usage", 0),
                        "mem": p_stats.get("memory_usage", 0),
                        "mem_percent": p_stats.get("mem_percentage", 0),
                        "world_name": server_name,
                        "world_size": Stats.get_world_size(server_path),
                        "server_port": server_port,
                        "int_ping_results": int_data,
                        "online": ping_data["online"],
                        "max": ping_data["max"],
                        "players": [],
                        "desc": ping_data["server_description"],
                        "version": ping_data["server_version"],
                        "icon": server_icon,
                    }
                else:
                    server_stats = {
                        "id": server_id,
                        "started": self.get_start_time(),
                        "running": self.check_running(),
                        "cpu": p_stats.get("cpu_usage", 0),
                        "mem": p_stats.get("memory_usage", 0),
                        "mem_percent": p_stats.get("mem_percentage", 0),
                        "world_name": server_name,
                        "world_size": Stats.get_world_size(server_path),
                        "server_port": server_port,
                        "int_ping_results": int_data,
                        "online": False,
                        "max": False,
                        "players": False,
                        "desc": False,
                        "version": False,
                        "icon": False,
                    }
        else:
            server_stats = {
                "id": server_id,
                "started": self.get_start_time(),
                "running": self.check_running(),
                "cpu": p_stats.get("cpu_usage", 0),
                "mem": p_stats.get("memory_usage", 0),
                "mem_percent": p_stats.get("mem_percentage", 0),
                "world_name": server_name,
                "world_size": Stats.get_world_size(server_path),
                "server_port": server_port,
                "int_ping_results": int_data,
                "online": False,
                "max": False,
                "players": False,
                "desc": False,
                "version": False,
            }

        return server_stats

    def record_server_stats(self):

        server = self.get_servers_stats()
        Server_Stats.insert(
            {
                Server_Stats.server_id: server.get("id", 0),
                Server_Stats.started: server.get("started", ""),
                Server_Stats.running: server.get("running", False),
                Server_Stats.cpu: server.get("cpu", 0),
                Server_Stats.mem: server.get("mem", 0),
                Server_Stats.mem_percent: server.get("mem_percent", 0),
                Server_Stats.world_name: server.get("world_name", ""),
                Server_Stats.world_size: server.get("world_size", ""),
                Server_Stats.server_port: server.get("server_port", ""),
                Server_Stats.int_ping_results: server.get("int_ping_results", False),
                Server_Stats.online: server.get("online", False),
                Server_Stats.max: server.get("max", False),
                Server_Stats.players: server.get("players", False),
                Server_Stats.desc: server.get("desc", False),
                Server_Stats.version: server.get("version", False),
            }
        ).execute()

        # delete old data
        max_age = self.helper.get_setting("history_max_age")
        now = datetime.datetime.now()
        last_week = now.day - max_age

        Server_Stats.delete().where(Server_Stats.created < last_week).execute()
