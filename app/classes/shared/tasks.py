import os
import sys
import json
import time
import logging
import threading

from app.classes.shared.helpers import helper
from app.classes.shared.console import console
from app.classes.web.tornado import webserver
from app.classes.web.websocket_handler import WebSocketHandler

from app.classes.minecraft.stats import stats
from app.classes.shared.controller import controller
from app.classes.minecraft.serverjars import server_jar_obj
from app.classes.shared.models import db_helper

logger = logging.getLogger(__name__)

try:
    import schedule

except ModuleNotFoundError as e:
    logger.critical("Import Error: Unable to load {} module".format(e, e.name))
    console.critical("Import Error: Unable to load {} module".format(e, e.name))
    sys.exit(1)


class TasksManager:

    def __init__(self):
        self.tornado = webserver()
        self.webserver_thread = threading.Thread(target=self.tornado.run_tornado, daemon=True, name='tornado_thread')

        self.main_kill_switch_thread = threading.Thread(target=self.main_kill_switch, daemon=True, name="main_loop")
        self.main_thread_exiting = False

        self.schedule_thread = threading.Thread(target=self.scheduler_thread, daemon=True, name="scheduler")

        self.command_thread = threading.Thread(target=self.command_watcher, daemon=True, name="command_watcher")
        self.command_thread.start()

        self.realtime_thread = threading.Thread(target=self.realtime_thread, daemon=True, name="realtime")
        self.realtime_thread.start()

    def get_main_thread_run_status(self):
        return self.main_thread_exiting

    def start_main_kill_switch_watcher(self):
        self.main_kill_switch_thread.start()

    def main_kill_switch(self):
        while True:
            if os.path.exists(os.path.join(helper.root_dir, 'exit.txt')):
                logger.info("Found Exit File, stopping everything")
                self._main_graceful_exit()
            time.sleep(5)

    @staticmethod
    def command_watcher():
        while True:
            # select any commands waiting to be processed
            commands = db_helper.get_unactioned_commands()
            for c in commands:

                svr = controller.get_server_obj(c['server_id']['server_id'])
                command = c.get('command', None)

                if command == 'start_server':
                    svr.run_threaded_server()

                elif command == 'stop_server':
                    svr.stop_threaded_server()

                elif command == "restart_server":
                    svr.restart_threaded_server()

                db_helper.mark_command_complete(c.get('command_id', None))

            time.sleep(1)

    def _main_graceful_exit(self):
        try:
            os.remove(helper.session_file)
            os.remove(os.path.join(helper.root_dir, 'exit.txt'))
            os.remove(os.path.join(helper.root_dir, '.header'))
            controller.stop_all_servers()
        except:
            pass

        logger.info("***** Crafty Shutting Down *****\n\n")
        console.info("***** Crafty Shutting Down *****\n\n")
        self.main_thread_exiting = True

    def start_webserver(self):
        self.webserver_thread.start()

    def reload_webserver(self):
        self.tornado.stop_web_server()
        console.info("Waiting 3 seconds")
        time.sleep(3)
        self.webserver_thread = threading.Thread(target=self.tornado.run_tornado, daemon=True, name='tornado_thread')
        self.start_webserver()

    def stop_webserver(self):
        self.tornado.stop_web_server()

    def start_scheduler(self):
        logger.info("Launching Scheduler Thread...")
        console.info("Launching Scheduler Thread...")
        self.schedule_thread.start()

    @staticmethod
    def scheduler_thread():
        while True:
            schedule.run_pending()
            time.sleep(1)

    @staticmethod
    def start_stats_recording():
        stats_update_frequency = helper.get_setting('stats_update_frequency')
        logger.info("Stats collection frequency set to {stats} seconds".format(stats=stats_update_frequency))
        console.info("Stats collection frequency set to {stats} seconds".format(stats=stats_update_frequency))

        # one for now,
        stats.record_stats()

        # one for later
        schedule.every(stats_update_frequency).seconds.do(stats.record_stats)

    @staticmethod
    def serverjar_cache_refresher():
        logger.info("Refreshing serverjars.com cache on start")
        server_jar_obj.refresh_cache()

        logger.info("Scheduling Serverjars.com cache refresh service every 12 hours")
        schedule.every(12).hours.do(server_jar_obj.refresh_cache)

    @staticmethod
    def realtime_thread():
        console.debug('realtime zero')


tasks_manager = TasksManager()
