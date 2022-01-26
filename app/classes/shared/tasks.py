import os
import sys
import time
import logging
import threading
import asyncio
from tzlocal import get_localzone

from app.classes.shared.helpers import helper
from app.classes.shared.console import console

from app.classes.web.tornado_handler import Webserver
from app.classes.web.websocket_helper import websocket_helper

from app.classes.minecraft.serverjars import server_jar_obj
from app.classes.models.management import management_helper
from app.classes.controllers.users_controller import Users_Controller
from app.classes.controllers.servers_controller import Servers_Controller

logger = logging.getLogger('apscheduler')

try:
    from apscheduler.events import EVENT_JOB_EXECUTED
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

except ModuleNotFoundError as err:
    logger.critical(f"Import Error: Unable to load {err.name} module", exc_info=True)
    console.critical(f"Import Error: Unable to load {err.name} module")
    sys.exit(1)

scheduler_intervals = { 'seconds',
                        'minutes',
                        'hours',
                        'days',
                        'weeks',
                        'monday',
                        'tuesday',
                        'wednesday',
                        'thursday',
                        'friday',
                        'saturday',
                        'sunday'
                        }

class TasksManager:

    def __init__(self, controller):
        self.controller = controller
        self.tornado = Webserver(controller, self)

        self.tz = get_localzone()
        self.scheduler = BackgroundScheduler(timezone=str(self.tz))

        self.users_controller = Users_Controller()

        self.webserver_thread = threading.Thread(target=self.tornado.run_tornado, daemon=True, name='tornado_thread')

        self.main_thread_exiting = False

        self.schedule_thread = threading.Thread(target=self.scheduler_thread, daemon=True, name="scheduler")

        self.log_watcher_thread = threading.Thread(target=self.log_watcher, daemon=True, name="log_watcher")

        self.command_thread = threading.Thread(target=self.command_watcher, daemon=True, name="command_watcher")

        self.realtime_thread = threading.Thread(target=self.realtime, daemon=True, name="realtime")

        self.reload_schedule_from_db()


    def get_main_thread_run_status(self):
        return self.main_thread_exiting

    def reload_schedule_from_db(self):
        jobs = management_helper.get_schedules_enabled()
        logger.info("Reload from DB called. Current enabled schedules: ")
        for item in jobs:
            logger.info(f"JOB: {item}")

    def command_watcher(self):
        while True:
            # select any commands waiting to be processed
            commands = management_helper.get_unactioned_commands()
            for c in commands:
                try:
                    svr = self.controller.get_server_obj(c.server_id)
                except:
                    logger.error("Server value requested does note exist purging item from waiting commands.")
                    management_helper.mark_command_complete(c.command_id)

                user_id = c.user_id
                command = c.command

                if command == 'start_server':
                    svr.run_threaded_server(user_id)

                elif command == 'stop_server':
                    svr.stop_threaded_server()

                elif command == "restart_server":
                    svr.restart_threaded_server(user_id)

                elif command == "backup_server":
                    svr.backup_server()

                elif command == "update_executable":
                    svr.jar_update()
                else:
                    svr.send_command(command)
                management_helper.mark_command_complete(c.command_id)

            time.sleep(1)

    def _main_graceful_exit(self):
        try:
            os.remove(helper.session_file)
            os.remove(os.path.join(helper.root_dir, '.header'))
            self.controller.stop_all_servers()
        except:
            logger.info("Caught error during shutdown", exc_info=True)

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
        logger.info("Launching command thread...")
        console.info("Launching command thread...")
        self.command_thread.start()
        logger.info("Launching log watcher...")
        console.info("Launching log watcher...")
        self.log_watcher_thread.start()
        logger.info("Launching realtime thread...")
        console.info("Launching realtime thread...")
        self.realtime_thread.start()

    def scheduler_thread(self):
        schedules = management_helper.get_schedules_enabled()
        self.scheduler.add_listener(self.schedule_watcher, mask=EVENT_JOB_EXECUTED)
        #self.scheduler.add_job(self.scheduler.print_jobs, 'interval', seconds=10, id='-1')

        #load schedules from DB
        for schedule in schedules:
            if schedule.cron_string != "":
                try:
                    self.scheduler.add_job(management_helper.add_command,
                                           CronTrigger.from_crontab(schedule.cron_string,
                                           timezone=str(self.tz)),
                                           id = str(schedule.schedule_id),
                                           args = [schedule.server_id,
                                                   self.users_controller.get_id_by_name('system'),
                                                   '127.0.0.1',
                                                   schedule.command]
                                           )
                except Exception as e:
                    console.error(f"Failed to schedule task with error: {e}.")
                    console.warning("Removing failed task from DB.")
                    logger.error(f"Failed to schedule task with error: {e}.")
                    logger.warning("Removing failed task from DB.")
                    #remove items from DB if task fails to add to apscheduler
                    management_helper.delete_scheduled_task(schedule.schedule_id)
            else:
                if schedule.interval_type == 'hours':
                    self.scheduler.add_job(management_helper.add_command,
                                           'cron',
                                           minute = 0,
                                           hour = '*/'+str(schedule.interval),
                                           id = str(schedule.schedule_id),
                                           args = [schedule.server_id,
                                                   self.users_controller.get_id_by_name('system'),
                                                   '127.0.0.1',
                                                   schedule.command]
                                           )
                elif schedule.interval_type == 'minutes':
                    self.scheduler.add_job(management_helper.add_command,
                                           'cron',
                                           minute = '*/'+str(schedule.interval),
                                           id = str(schedule.schedule_id),
                                           args = [schedule.server_id,
                                                   self.users_controller.get_id_by_name('system'),
                                                   '127.0.0.1',
                                                   schedule.command]
                                           )
                elif schedule.interval_type == 'days':
                    curr_time = schedule.start_time.split(':')
                    self.scheduler.add_job(management_helper.add_command,
                                           'cron',
                                           day = '*/'+str(schedule.interval),
                                           hour=curr_time[0],
                                           minute=curr_time[1],
                                           id=str(schedule.schedule_id),
                                           args=[schedule.server_id,
                                                 self.users_controller.get_id_by_name('system'),
                                                 '127.0.0.1',
                                                 schedule.command]
                                           )
        self.scheduler.start()
        jobs = self.scheduler.get_jobs()
        logger.info("Loaded schedules. Current enabled schedules: ")
        for item in jobs:
            logger.info(f"JOB: {item}")

    def schedule_job(self, job_data):
        sch_id = management_helper.create_scheduled_task(
            job_data['server_id'],
            job_data['action'],
            job_data['interval'],
            job_data['interval_type'],
            job_data['start_time'],
            job_data['command'],
            "None",
            job_data['enabled'],
            job_data['one_time'],
            job_data['cron_string'])
        if job_data['enabled']:
            if job_data['cron_string'] != "":
                try:
                    self.scheduler.add_job(management_helper.add_command,
                                           CronTrigger.from_crontab(job_data['cron_string'],
                                           timezone=str(self.tz)),
                                           id=str(sch_id),
                                           args=[job_data['server_id'],
                                                 self.users_controller.get_id_by_name('system'),
                                                 '127.0.0.1',
                                                 job_data['command']]
                                           )
                except Exception as e:
                    console.error(f"Failed to schedule task with error: {e}.")
                    console.warning("Removing failed task from DB.")
                    logger.error(f"Failed to schedule task with error: {e}.")
                    logger.warning("Removing failed task from DB.")
                    #remove items from DB if task fails to add to apscheduler
                    management_helper.delete_scheduled_task(sch_id)
            else:
                if job_data['interval_type'] == 'hours':
                    self.scheduler.add_job(management_helper.add_command,
                                           'cron',
                                           minute = 0,
                                           hour = '*/'+str(job_data['interval']),
                                           id=str(sch_id),
                                           args=[job_data['server_id'],
                                                 self.users_controller.get_id_by_name('system'),
                                                 '127.0.0.1',
                                                 job_data['command']]
                                           )
                elif job_data['interval_type'] == 'minutes':
                    self.scheduler.add_job(management_helper.add_command,
                                           'cron',
                                           minute = '*/'+str(job_data['interval']),
                                           id=str(sch_id),
                                           args=[job_data['server_id'],
                                                 self.users_controller.get_id_by_name('system'),
                                                 '127.0.0.1',
                                                 job_data['command']]
                                           )
                elif job_data['interval_type'] == 'days':
                    curr_time = job_data['start_time'].split(':')
                    self.scheduler.add_job(management_helper.add_command,
                                           'cron',
                                           day = '*/'+str(job_data['interval']),
                                           hour = curr_time[0],
                                           minute = curr_time[1],
                                           id=str(sch_id),
                                           args=[job_data['server_id'],
                                                 self.users_controller.get_id_by_name('system'),
                                                 '127.0.0.1',
                                                 job_data['command']],
                                           )
            logger.info("Added job. Current enabled schedules: ")
            jobs = self.scheduler.get_jobs()
            for item in jobs:
                logger.info(f"JOB: {item}")

    def remove_all_server_tasks(self, server_id):
        schedules = management_helper.get_schedules_by_server(server_id)
        for schedule in schedules:
            self.remove_job(schedule.schedule_id)

    def remove_job(self, sch_id):
        job = management_helper.get_scheduled_task_model(sch_id)
        management_helper.delete_scheduled_task(sch_id)
        if job.enabled:
            self.scheduler.remove_job(str(sch_id))
            logger.info(f"Job with ID {sch_id} was deleted.")
        else:
            logger.info(f"Job with ID {sch_id} was deleted from DB, but was not enabled."
                        + "Not going to try removing something that doesn't exist from active schedules.")

    def update_job(self, sch_id, job_data):
        management_helper.update_scheduled_task(sch_id, job_data)
        try:
            self.scheduler.remove_job(str(sch_id))
        except:
            logger.info("No job found in update job. Assuming it was previously disabled. Starting new job.")

        if job_data['enabled']:
            if job_data['cron_string'] != "":
                try:
                    self.scheduler.add_job(management_helper.add_command,
                                           CronTrigger.from_crontab(job_data['cron_string'],
                                           timezone=str(self.tz)),
                                           id=str(sch_id),
                                           args=[job_data['server_id'],
                                                 self.users_controller.get_id_by_name('system'),
                                                 '127.0.0.1',
                                                 job_data['command']]
                                           )
                except Exception as e:
                    console.error(f"Failed to schedule task with error: {e}.")
                    console.info("Removing failed task from DB.")
                    management_helper.delete_scheduled_task(sch_id)
            else:
                if job_data['interval_type'] == 'hours':
                    self.scheduler.add_job(management_helper.add_command,
                                           'cron',
                                           minute = 0,
                                           hour = '*/'+str(job_data['interval']),
                                           id=str(sch_id),
                                           args=[job_data['server_id'],
                                                 self.users_controller.get_id_by_name('system'),
                                                 '127.0.0.1',
                                                 job_data['command']]
                                           )
                elif job_data['interval_type'] == 'minutes':
                    self.scheduler.add_job(management_helper.add_command,
                                           'cron',
                                           minute = '*/'+str(job_data['interval']),
                                           id=str(sch_id),
                                           args=[job_data['server_id'],
                                                 self.users_controller.get_id_by_name('system'),
                                                 '127.0.0.1',
                                                 job_data['command']]
                                           )
                elif job_data['interval_type'] == 'days':
                    curr_time = job_data['start_time'].split(':')
                    self.scheduler.add_job(management_helper.add_command,
                                           'cron',
                                           day = '*/'+str(job_data['interval']),
                                           hour = curr_time[0],
                                           minute = curr_time[1],
                                           id=str(sch_id),
                                           args=[job_data['server_id'],
                                                 self.users_controller.get_id_by_name('system'),
                                                 '127.0.0.1',
                                                 job_data['command']]
                                           )
        else:
            try:
                self.scheduler.get_job(str(sch_id))
                self.scheduler.remove_job(str(sch_id))
            except:
                logger.info(f"APScheduler found no scheduled job on schedule update for schedule with id: {sch_id} Assuming it was already disabled.")

    def schedule_watcher(self, event):
        if not event.exception:
            if str(event.job_id).isnumeric():
                task = management_helper.get_scheduled_task_model(int(event.job_id))
                if task.one_time:
                    self.remove_job(task.schedule_id)
                    logger.info("one time task detected. Deleting...")
            else:
                logger.info("Event job ID is not numerical. Assuming it's stats - not stored in DB. Moving on.")
        else:
            logger.error(f"Task failed with error: {event.exception}")

    def start_stats_recording(self):
        stats_update_frequency = helper.get_setting('stats_update_frequency')
        logger.info(f"Stats collection frequency set to {stats_update_frequency} seconds")
        console.info(f"Stats collection frequency set to {stats_update_frequency} seconds")

        # one for now,
        self.controller.stats.record_stats()
        # one for later
        self.scheduler.add_job(self.controller.stats.record_stats, 'interval', seconds=stats_update_frequency, id="stats")


    def serverjar_cache_refresher(self):
        logger.info("Refreshing serverjars.com cache on start")
        server_jar_obj.refresh_cache()

        logger.info("Scheduling Serverjars.com cache refresh service every 12 hours")
        self.scheduler.add_job(server_jar_obj.refresh_cache, 'interval', hours=12, id="serverjars")

    def realtime(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        host_stats = management_helper.get_latest_hosts_stats()

        while True:

            if host_stats.get('cpu_usage') != \
                    management_helper.get_latest_hosts_stats().get('cpu_usage') or \
                    host_stats.get('mem_percent') != \
                    management_helper.get_latest_hosts_stats().get('mem_percent'):
                # Stats are different

                host_stats = management_helper.get_latest_hosts_stats()
                if len(websocket_helper.clients) > 0:
                    # There are clients
                    websocket_helper.broadcast_page('/panel/dashboard', 'update_host_stats', {
                        'cpu_usage': host_stats.get('cpu_usage'),
                        'cpu_cores': host_stats.get('cpu_cores'),
                        'cpu_cur_freq': host_stats.get('cpu_cur_freq'),
                        'cpu_max_freq': host_stats.get('cpu_max_freq'),
                        'mem_percent': host_stats.get('mem_percent'),
                        'mem_usage': host_stats.get('mem_usage')
                    })

            for user in Users_Controller.get_all_users():
                servers_ping = []
                if user.superuser:
                    servers = Servers_Controller.get_all_servers_stats()
                else:
                    servers = Servers_Controller.get_authorized_servers_stats(user.user_id)
                for srv in servers:
                    if srv:
                        server_id = srv['server_data']['server_id']
                        srv['raw_ping_result'] = self.controller.stats.get_raw_server_stats(server_id)
                        if f"{srv['raw_ping_result'].get('icon')}" == "b''":
                            srv['raw_ping_result']['icon'] = False

                        servers_ping.append({
                            'id': srv['raw_ping_result'].get('id'),
                            'started': srv['raw_ping_result'].get('started'),
                            'running': srv['raw_ping_result'].get('running'),
                            'cpu': srv['raw_ping_result'].get('cpu'),
                            'mem': srv['raw_ping_result'].get('mem'),
                            'mem_percent': srv['raw_ping_result'].get('mem_percent'),
                            'world_name': srv['raw_ping_result'].get('world_name'),
                            'world_size': srv['raw_ping_result'].get('world_size'),
                            'server_port': srv['raw_ping_result'].get('server_port'),
                            'int_ping_results': srv['raw_ping_result'].get('int_ping_results'),
                            'online': srv['raw_ping_result'].get('online'),
                            'max': srv['raw_ping_result'].get('max'),
                            'players': srv['raw_ping_result'].get('players'),
                            'desc': srv['raw_ping_result'].get('desc'),
                            'version': srv['raw_ping_result'].get('version'),
                            'icon': None
                        })
                        if len(websocket_helper.clients) > 0:
                            websocket_helper.broadcast_user_page_params(
                                '/panel/server_detail',
                                {
                                    'id': str(server_id)
                                }, user.user_id,
                                'update_server_details',
                                {
                                    'id': srv['raw_ping_result'].get('id'),
                                    'started': srv['raw_ping_result'].get('started'),
                                    'running': srv['raw_ping_result'].get('running'),
                                    'cpu': srv['raw_ping_result'].get('cpu'),
                                    'mem': srv['raw_ping_result'].get('mem'),
                                    'mem_percent': srv['raw_ping_result'].get('mem_percent'),
                                    'world_name': srv['raw_ping_result'].get('world_name'),
                                    'world_size': srv['raw_ping_result'].get('world_size'),
                                    'server_port': srv['raw_ping_result'].get('server_port'),
                                    'int_ping_results': srv['raw_ping_result'].get('int_ping_results'),
                                    'online': srv['raw_ping_result'].get('online'),
                                    'max': srv['raw_ping_result'].get('max'),
                                    'players': srv['raw_ping_result'].get('players'),
                                    'desc': srv['raw_ping_result'].get('desc'),
                                    'version': srv['raw_ping_result'].get('version'),
                                    'icon': None
                                }
                            )

                if (len(servers_ping) > 0) & (len(websocket_helper.clients) > 0):
                    try:
                        websocket_helper.broadcast_user_page('/panel/dashboard', user.user_id, 'update_server_status', servers_ping)
                        websocket_helper.broadcast_page('/status', 'update_server_status', servers_ping)
                    except:
                        console.warning("Can't broadcast server status to websocket")
            time.sleep(5)

    def log_watcher(self):
        self.controller.servers.check_for_old_logs()
        self.scheduler.add_job(self.controller.servers.check_for_old_logs, 'interval', hours=6, id="log-mgmt")
